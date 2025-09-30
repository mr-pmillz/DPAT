#!/usr/bin/python

import webbrowser
import io
import os
import re
import argparse
import sqlite3
from shutil import copyfile
import html
import binascii
import hashlib
from typing import Iterable, Optional, Sequence, Union

# Custom strtobool replacement since distutils is deprecated
def strtobool(val):
    """Convert a string representation of truth to True or False."""
    val = val.lower()
    if val in ('y', 'yes', 't', 'true', 'on', '1'):
        return True
    elif val in ('n', 'no', 'f', 'false', 'off', '0'):
        return False
    else:
        raise ValueError("invalid truth value %r" % (val,))
filename_for_html_report = "_DomainPasswordAuditReport.html"
folder_for_html_report = "DPAT Report"
filename_for_db_on_disk = "pass_audit.db"
compare_groups = []

_NTDS_PATTERNS = [
    # DOMAIN\user:rest
    re.compile(r'^(?P<domain>[^\\]+)\\(?P<user>[^:]+):(?P<nt>[0-9A-Fa-f]{32}).*$', re.I),
    # pwdump style
    re.compile(r'^(?P<user>[^:]+):(?P<rid>\d+):(?P<lm>[0-9A-Fa-f]{32}|\*):(?P<nt>[0-9A-Fa-f]{32}|\*):.*$', re.I),
]
def _parse_ntds(line: str):
    for pat in _NTDS_PATTERNS:
        m = pat.match(line)
        if m:
            return m.group('user').lower(), m.group('nt').lower()
    return None, None

def load_kerberoast_ntds(path: str, enc: str = 'cp1252', debug: bool = False):
    """
    Returns rows of tuples  (username_full, nt_hash)
    """
    kerb_entries = []
    with open(path, 'r', encoding=enc, errors='replace') as f:
        for i, raw in enumerate(f, 1):
            user, nt = _parse_ntds(raw.strip())
            if user and nt and nt != '*' * 32:
                kerb_entries.append((user, nt))
                if debug:
                    print(f"[kerb DEBUG] line {i}: {user}:{nt}")
            elif debug:
                print(f"[kerb DEBUG] line {i}: skipped")
    return kerb_entries

# This should be False as it is only a shortcut used during development
speed_it_up = False

parser = argparse.ArgumentParser(
    description='This script will perform a domain password audit based on an extracted NTDS file and password cracking output such as Hashcat.')
parser.add_argument('-n', '--ntdsfile',
                    help='NTDS file name (output from SecretsDump.py)', required=True)
parser.add_argument('-c', '--crackfile',
                    help='Password Cracking output in the default form output by Hashcat, such as hashcat.potfile', required=True)
parser.add_argument('-o', '--outputfile', help='The name of the HTML report output file, defaults to ' +
                    filename_for_html_report, required=False, default=filename_for_html_report)
parser.add_argument('-d', '--reportdirectory', help='Folder containing the output HTML files, defaults to ' +
                    folder_for_html_report, required=False, default=folder_for_html_report)
parser.add_argument('-w', '--writedb', help='Write the SQLite database info to disk for offline inspection instead of just in memory. Filename will be "' +
                    filename_for_db_on_disk + '"', default=False, required=False, action='store_true')
parser.add_argument('-s', '--sanitize', help='Sanitize the report by partially redacting passwords and hashes. Prepends the report directory with \"Sanitized - \"',
                    default=False, required=False, action='store_true')
parser.add_argument('-g', '--groupsdirectory', help='The path to the directory containing files that contain lists of usernames in particular groups. The group ' +
                    'names will be taken from the first line in each file. The username list must be in the same format as found in the NTDS file such as ' +
                    'some.ad.domain.com\\username', required=False)
parser.add_argument('-m', '--machineaccts', help='Include machine accounts when calculating statistics',
                    default=False, required=False, action='store_true')
parser.add_argument('-k', '--krbtgt', help='Include the krbtgt account', default=False, required=False, action='store_true')
parser.add_argument('-kz', '--kerbfile',
        help='File that contains NTDS lines for Kerberoastable accounts (from the cypherhound script)',
        required=False)
parser.add_argument('--ch-encoding',
        help='Encoding to open cypherhound files with (default cp1252)',
        default='cp1252', required=False)
parser.add_argument('-dbg', '--debug',
        help='Enable debug output (for development purposes)',
        default=False, required=False, action='store_true')
parser.add_argument('-p', '--minpasslen',
    type=int,
    help='Minimum password length defined in the domain password policy. '
         'Any cracked password shorter than this is reported.',
    required=True)
args = parser.parse_args()

min_len = args.minpasslen
ntds_file = args.ntdsfile
cracked_file = args.crackfile
filename_for_html_report = args.outputfile
folder_for_html_report = args.reportdirectory
if args.sanitize:
    folder_for_html_report = folder_for_html_report + " - Sanitized"
if args.groupsdirectory is not None:
    group_dir = os.path.normpath(args.groupsdirectory)
    print(f"[+] Groups directory specified: {group_dir}")

    if os.path.isdir(group_dir):
        print(f"[+] Loading group files from directory: {group_dir}")
        for fname in sorted(os.listdir(group_dir)):
            fpath = os.path.join(group_dir, fname)
            print(f"  ├─ Processing file: {fname}")
            if os.path.isfile(fpath):
                try:
                    with open(fpath, 'r', encoding='cp1252' if not args.ch_encoding else args.ch_encoding) as f:
                        first_line = f.readline().strip()
                        print(f"  ├─ First line: '{first_line}'")
                        if first_line:
                            # Use filename (without extension) as group name
                            group_name = os.path.splitext(fname)[0]
                            compare_groups.append((group_name, fpath))
                            print(f"  └─ Loaded group '{group_name}' from file: {fname}")
                        else:
                            print(f"  └─ Skipped empty file: {fname}")
                except Exception as e:
                    print(f"[!] Error reading file {fpath}: {e}")
    else:
        print(f"[!] Specified groupsdirectory is not a valid directory: {group_dir}")    

# create report folder if it doesn't already exist
if not os.path.exists(folder_for_html_report):
    os.makedirs(folder_for_html_report)

# percentage calculation helper function
def pct(part, whole):
    try:
        return round((part / whole) * 100, 2)
    except ZeroDivisionError:
        return 0.0

# show only the first and last char of a password or a few more chars for a hash
def sanitize(pass_or_hash):
    if not args.sanitize:
        return pass_or_hash
    else:
        sanitized_string = pass_or_hash
        lenp = len(pass_or_hash)
        if lenp == 32:
            # For 32-char hashes: show first 4 and last 4 chars
            sanitized_string = pass_or_hash[0:4] + \
                "*"*(lenp-8) + pass_or_hash[lenp-4:lenp]
        elif lenp > 2:
            # For other strings: show first and last char
            sanitized_string = pass_or_hash[0] + \
                "*"*(lenp-2) + pass_or_hash[lenp-1]
        return sanitized_string

def sanitize_table_row(row, password_indices, hash_indices):
    """
    Sanitize passwords and hashes in table rows.
    password_indices: list of column indices containing passwords
    hash_indices: list of column indices containing hashes
    """
    if not args.sanitize:
        return row
    
    sanitized_row = list(row)
    for idx in password_indices:
        if idx < len(sanitized_row) and sanitized_row[idx] is not None:
            sanitized_row[idx] = sanitize(str(sanitized_row[idx]))
    for idx in hash_indices:
        if idx < len(sanitized_row) and sanitized_row[idx] is not None:
            sanitized_row[idx] = sanitize(str(sanitized_row[idx]))
    
    return tuple(sanitized_row)


class HtmlBuilder:
    bodyStr = ""

    def build_html_body_string(self, s: str):
        self.bodyStr += s + "\n<div class='section-space'></div>\n"

    def get_html(self):
        return (
            "<!DOCTYPE html>\n<html>\n<head>\n"
            "<meta charset='utf-8'>\n<meta name='viewport' content='width=device-width,initial-scale=1'>\n"
            "<link rel='stylesheet' href='report.css'>\n"
            "<title>DPAT Report</title>\n"
            "</head>\n<body>\n"
            + self.bodyStr +
            "\n</body>\n</html>\n"
        )

    def add_table_to_html(
        self,
        rows: Iterable[Sequence[object]],
        headers: Sequence[str] = (),
        cols_to_not_escape: Union[int, Sequence[int], None] = (),
        caption: Optional[str] = None
    ):
        if cols_to_not_escape is None:
            cols_to_not_escape = set()
        elif isinstance(cols_to_not_escape, int):
            cols_to_not_escape = {cols_to_not_escape}
        else:
            cols_to_not_escape = set(cols_to_not_escape)

        out = ["<div class='table-wrap'>", "<table class='report'>"]
        if caption:
            out.append(f"<caption>{html.escape(caption)}</caption>")

        # Header
        out.append("<thead><tr>")
        for h in headers:
            out.append(f"<th>{'' if h is None else html.escape(str(h))}</th>")
        out.append("</tr></thead>")

        # Body
        out.append("<tbody>")
        for row in rows:
            out.append("<tr>")
            for idx, cell in enumerate(row):
                cell_data = "" if cell is None else str(cell)
                if idx not in cols_to_not_escape:
                    cell_data = html.escape(cell_data)
                out.append(f"<td>{cell_data}</td>")
            out.append("</tr>")
        out.append("</tbody></table></div>")
        self.build_html_body_string("".join(out))

    def write_html_report(self, filename):
        with open(os.path.join(folder_for_html_report, filename), "w", encoding="utf-8") as f:
            copyfile(os.path.join(os.path.dirname(__file__), "report.css"),
                     os.path.join(folder_for_html_report, "report.css"))
            f.write(self.get_html())
        return filename

hb = HtmlBuilder()
summary_table = []
summary_table_headers = ("Count", "Percent", "Description", "More Info")

conn = sqlite3.connect(':memory:')
if args.writedb:
    if os.path.exists(filename_for_db_on_disk):
        os.remove(filename_for_db_on_disk)
    conn = sqlite3.connect(filename_for_db_on_disk)
if speed_it_up:
    conn = sqlite3.connect(filename_for_db_on_disk)
conn.text_factory = str
c = conn.cursor()

# nt2lmcrack functionality
# the all_casings functionality was taken from https://github.com/BBerastegui/foo/blob/master/casing.py
def all_casings(input_string):
    if not input_string:
        yield ""
    else:
        first = input_string[:1]
        if first.lower() == first.upper():
            for sub_casing in all_casings(input_string[1:]):
                yield first + sub_casing
        else:
            for sub_casing in all_casings(input_string[1:]):
                yield first.lower() + sub_casing
                yield first.upper() + sub_casing


def crack_it(nt_hash, lm_pass):
    password = None
    for pwd_guess in all_casings(lm_pass):
        try:
            hash = ntlm_hash(pwd_guess)
            if nt_hash.lower() == hash.lower():
                password = pwd_guess
                break
        except RuntimeError as e:
            print(f"[!] NT hash backend unavailable for cracking: {e}")
            break
    return password


# NTLM hash function
def ntlm_hash(s: str) -> str:
    """Return NT hash (MD4 over UTF-16LE) of a string as lowercase hex."""
    data = s.encode('utf-16le')

    # 1) hashlib (often unavailable for MD4)
    try:
        import hashlib
        return hashlib.new('md4', data).hexdigest().lower()
    except Exception as e:
        print(f"[DEBUG] hashlib md4 unavailable: {e}")

    # 2) pycryptodome → Crypto.*
    try:
        from Crypto.Hash import MD4
        return MD4.new(data).hexdigest().lower()
    except Exception as e:
        print(f"[DEBUG] PyCryptodome (Crypto) MD4 failed: {e}")

    # 3) pycryptodomex (alternative) → Cryptodome.*
    try:
        from Cryptodome.Hash import MD4
        return MD4.new(data).hexdigest().lower()
    except Exception as e:
        print(f"[DEBUG] PyCryptodomex (Cryptodome) MD4 failed: {e}")

    # 4) passlib
    try:
        from passlib.hash import nthash
        return nthash.hash(s).lower()
    except Exception as e:
        print(f"[DEBUG] passlib nthash failed: {e}")

    # 5) impacket
    try:
        from impacket.ntlm import compute_nthash
        return compute_nthash(s).hex().lower()
    except Exception as e:
        print(f"[DEBUG] impacket compute_nthash failed: {e}")

    raise RuntimeError("No NT hash backend available. Install pycryptodome (or pycryptodomex) / passlib / impacket.")


def username_candidates(u: str, u_full: str | None = None) -> set[str]:
    """
    Expand plausible username strings that users might set as their password:
    - raw username
    - DOMAIN\\user -> user
    - user@domain -> user
    - case variants (lower/upper/capitalize)
    """
    bases: set[str] = set()
    for val in (u, u_full):
        if not val:
            continue
        val = val.strip()
        if not val:
            continue
        bases.add(val)
        if '\\' in val:             # DOMAIN\user
            bases.add(val.split('\\', 1)[1])
        if '@' in val:              # user@domain
            bases.add(val.split('@', 1)[0])

    # Normalize and generate simple case variants
    bases = {b for b in (x.strip() for x in bases) if b}
    cands: set[str] = set()
    for b in bases:
        cands.add(b)
        cands.add(b.lower())
        cands.add(b.upper())
        cands.add(b.capitalize())
    return cands


if not speed_it_up:
    # Create tables and indices
    c.execute('''CREATE TABLE hash_infos
        (username_full text collate nocase, username text collate nocase, lm_hash text, lm_hash_left text, lm_hash_right text, nt_hash text, password text, lm_pass_left text, lm_pass_right text, only_lm_cracked boolean, history_index int, history_base_username text)''')
    c.execute("CREATE INDEX index_nt_hash ON hash_infos (nt_hash);")
    c.execute("CREATE INDEX index_lm_hash_left ON hash_infos (lm_hash_left);")
    c.execute("CREATE INDEX index_lm_hash_right ON hash_infos (lm_hash_right);")
    c.execute("CREATE INDEX lm_hash ON hash_infos (lm_hash);")
    c.execute("CREATE INDEX username ON hash_infos (username);")

    # Create boolean column for each group
    for group in compare_groups:
        sql = "ALTER TABLE hash_infos ADD COLUMN \"" + group[0] + "\" boolean"
        c.execute(sql)

    # Read users from each group; groups_users is a dictionary with key = group name and value = list of users
    groups_users = {}
    for group in compare_groups:
        user_domain = ""
        user_name = ""
        try:
            users = []
            fing = io.open(group[1], encoding='utf-16')
            for line in fing:
                if "MemberDomain" in line:
                    user_domain = (line.split(":")[1]).strip()
                if "MemberName" in line:
                    user_name = (line.split(":")[1]).strip()
                    users.append(user_domain + "\\" + user_name)
            if len(users) != 0:
                fing.close()
            else:
                print("Doesn't look like the Group Files are in the form output by PowerView, assuming the files are already in domain\\username list form")
                # If the users array is empty, assume the file was not in the PowerView PowerShell script output format that you get from running:
                # Get-NetGroupMember -GroupName "Enterprise Admins" -Domain "some.domain.com" -DomainController "DC01.some.domain.com" > Enterprise Admins.txt
                # You can list domain controllers for use in the above command with Get-NetForestDomain
            
                fing.seek(0)
                # Reset File pointer to first line and try again
                for line in fing:
                    users.append(line.rstrip("\n"))
                fing.close()
        except:
            fing.close()
            print("unknown exception while processing group file(s)")
        groups_users[group[0]] = users

    # Read in NTDS file
    print(f"[+] Reading NTDS file: {ntds_file}")
    fin = open(ntds_file)
    accounts_read = 0
    accounts_filtered = 0
    for line in fin:
        vals = line.rstrip("\n").split(':')
        if len(vals) == 1:
            continue
        accounts_read += 1
        usernameFull = vals[0]
        lm_hash = vals[2]
        lm_hash_left = lm_hash[0:16]
        lm_hash_right = lm_hash[16:32]
        nt_hash = vals[3]
        username = usernameFull.split('\\')[-1]
        history_base_username = usernameFull
        history_index = -1
        username_info = r"(?i)(.*\\*.*)_history([0-9]+)$"
        results = re.search(username_info,usernameFull)
        if results:
            history_base_username = results.group(1)
            history_index = results.group(2)
        # Exclude machine accounts (where account name ends in $) by default
        # Exclude krbtgt account by default to protect this infrequently changing password from unnecesary disclosure, issue #10
        if (args.machineaccts or not username.endswith("$")) and (args.krbtgt or not username == "krbtgt"):
            c.execute("INSERT INTO hash_infos (username_full, username, lm_hash , lm_hash_left , lm_hash_right , nt_hash, history_index, history_base_username) VALUES (?,?,?,?,?,?,?,?)",
                    (usernameFull, username, lm_hash, lm_hash_left, lm_hash_right, nt_hash, history_index, history_base_username))
        else:
            accounts_filtered += 1
    fin.close()
    
    # Count total accounts processed
    c.execute('SELECT count(*) FROM hash_infos WHERE history_index = -1')
    total_accounts_processed = c.fetchone()[0]
    print(f"[+] Read {accounts_read} accounts from NTDS file")
    if accounts_filtered > 0:
        print(f"[+] Filtered out {accounts_filtered} accounts (machine accounts, krbtgt)")
    print(f"[+] Processing {total_accounts_processed} accounts for analysis")

    # update group membership flags
    for group in groups_users:
        for user in groups_users[group]:
            sql = "UPDATE hash_infos SET \"{}\" = 1 WHERE username_full = ?".format(group)
            c.execute(sql, (user,))

    # read in POT file
    fin = open(cracked_file)
    for lineT in fin:
        line = lineT.rstrip('\r\n')
        colon_index = line.find(":")
        hash = line[0:colon_index]
        # Stripping $NT$ and $LM$ that is included in John the Ripper output by default
        jtr = False
        if hash.startswith('$NT$') or hash.startswith('$LM$'):
            hash = hash.lstrip("$NT$")
            hash = hash.lstrip("$LM$")
            jtr = True
        password = line[colon_index+1:len(line)]
        lenxx = len(hash)
        if re.match(r"\$HEX\[([^\]]+)", password) and not jtr:
            hex2 = (binascii.unhexlify(re.findall(r"\$HEX\[([^\]]+)", password)[-1]))
            l = list()
            for x in list(hex2):
                if type(x) == int:
                    x = str(chr(x))
                l.append(x)
            password = ""
            password = password.join(l)
        if lenxx == 32:  # An NT hash
            c.execute("UPDATE hash_infos SET password = ? WHERE nt_hash = ?", (password, hash))
        elif lenxx == 16:  # An LM hash, either left or right
            c.execute("UPDATE hash_infos SET lm_pass_left = ? WHERE lm_hash_left = ?", (password, hash))
            c.execute("UPDATE hash_infos SET lm_pass_right = ? WHERE lm_hash_right = ?", (password, hash))
    fin.close()

    # Do additional LM cracking
    c.execute('SELECT nt_hash,lm_pass_left,lm_pass_right FROM hash_infos WHERE (lm_pass_left is not NULL or lm_pass_right is not NULL) and password is NULL and lm_hash is not "aad3b435b51404eeaad3b435b51404ee" group by nt_hash')
    rows = c.fetchall()
    count = len(rows)
    if count != 0:
        print("Cracking %d NT Hashes where only LM Hash was cracked (aka lm2ntcrack functionality)" % count)
    for pair in rows:
        lm_pwd = ""
        if pair[1] is not None:
            lm_pwd += pair[1]
        if pair[2] is not None:
            lm_pwd += pair[2]
        password = crack_it(pair[0], lm_pwd)
        if password is not None:
            c.execute('UPDATE hash_infos SET only_lm_cracked = 1, password = \'' + password.replace("'", "''") + '\' WHERE nt_hash = \'' + pair[0] + '\'')
        count -= 1

# Total number of hashes in the NTDS file
c.execute('SELECT username_full,password,LENGTH(password) as plen,nt_hash,only_lm_cracked FROM hash_infos WHERE history_index = -1 ORDER BY plen DESC, password')
rows = c.fetchall()

# Sanitize passwords and hashes in the data
sanitized_rows = [sanitize_table_row(row, [1], [3]) for row in rows]  # password at index 1, nt_hash at index 3

num_hashes = len(rows)
hbt = HtmlBuilder()
hbt.add_table_to_html(
    sanitized_rows, ["Username", "Password", "Password Length", "NT Hash", "Only LM Cracked"])
filename = hbt.write_html_report("all hashes.html")
summary_table.append((num_hashes, None, "Password Hashes",
                      "<a href=\"" + filename + "\">Details</a>"))

# Check if we have any hashes to process
if num_hashes == 0:
    print("[!] Warning: No password hashes found in NTDS file. This may be due to:")
    print("    - All accounts being filtered out (machine accounts, krbtgt)")
    print("    - Empty or invalid NTDS file")
    print("    - Incorrect file format")
    print("[*] Exiting gracefully...")
    exit(0)

# Total number of UNIQUE hashes in the NTDS file
c.execute('SELECT count(DISTINCT nt_hash) FROM hash_infos WHERE history_index = -1')
num_unique_nt_hashes = c.fetchone()[0]
percent_unique = pct(num_unique_nt_hashes, num_hashes)
summary_table.append((num_unique_nt_hashes, percent_unique, "Unique Password Hashes", None))

# Calculate total number of duplicate password hashes
num_duplicate_hashes = num_hashes - num_unique_nt_hashes
percent_duplicate_hashes = pct(num_duplicate_hashes, num_hashes)

summary_table.append((
    num_duplicate_hashes,
    percent_duplicate_hashes,
    "Duplicate Password Hashes Identified Through Audit",
    None
))

# Number of users whose passwords were cracked
c.execute('SELECT count(*) FROM hash_infos WHERE password is not NULL AND history_index = -1')
num_passwords_cracked = c.fetchone()[0]
percent_all_cracked = pct(num_passwords_cracked, num_hashes)
summary_table.append(
    (num_passwords_cracked, percent_all_cracked, "Passwords Discovered Through Cracking", None))

# Number of UNIQUE passwords that were cracked
c.execute(
    'SELECT count(Distinct password) FROM hash_infos where password is not NULL AND history_index = -1 ')
num_unique_passwords_cracked = c.fetchone()[0]
percent_cracked_unique = pct(num_unique_passwords_cracked, num_hashes)
summary_table.append((num_unique_passwords_cracked, percent_cracked_unique,
                      "Unique Passwords Discovered Through Cracking", None))

# Kerberoastable Accounts
if args.kerbfile:
    print(f"[+] Processing Kerberoastable file: {args.kerbfile}")
    kerb_rows = load_kerberoast_ntds(args.kerbfile, args.ch_encoding, args.debug)

    if kerb_rows:
        # ---- NEW: pull usernames, not hashes ---------------------------
        kerb_usernames = tuple({u for u, _ in kerb_rows})   # de‑dupe set → tuple
        total_kerb_accts = len(kerb_usernames)
        placeholders = ",".join("?" * total_kerb_accts)

        c.execute(f'''
            SELECT username_full, nt_hash, password
            FROM   hash_infos
            WHERE  username_full IN ({placeholders})
              AND  password IS NOT NULL
              AND  history_index = -1
        ''', kerb_usernames)
        cracked_rows = c.fetchall()
        # ----------------------------------------------------------------

        if cracked_rows:
            # Sanitize passwords and hashes in the data
            sanitized_kerb_rows = [sanitize_table_row(row, [2], [1]) for row in cracked_rows]  # password at index 2, nt_hash at index 1
            
            kerb_report_builder = HtmlBuilder()
            kerb_headers = ("Username", "NT Hash", "Password")
            kerb_report_builder.add_table_to_html(sanitized_kerb_rows, kerb_headers, 2)
            kerb_filename = kerb_report_builder.write_html_report("kerberoast_cracked.html")

            # percentage of roastable accounts that are cracked
            percent = pct(len(cracked_rows), num_hashes)

            summary_table.append((
                len(cracked_rows), percent,
                "Cracked Kerberoastable Accounts",
                f'<a href="{kerb_filename}">Details</a>'
            ))
            print(f"[+] Kerberoast cracked report written: {kerb_filename} "
                  f"({len(cracked_rows)} / {num_hashes} = {percent}% cracked)")
        else:
            print("[+] No Kerberoastable accounts were cracked.")
    else:
        print("[!] Kerberoastable file contained no valid NTDS lines.")

# Group Membership Details and number of passwords cracked for each group
# We'll collect rows for the single groups page here:
group_summary_rows = []
group_page_headers = ("Group Name",
                      "# Members",
                      "# Passwords Cracked",
                      "% Cracked",
                      "Members Details",
                      "Cracked PW Details")

# --- GROUP MEMBERSHIP DETAILS LOOP ---
for group in compare_groups:
    group_name = group[0]

    # 1) Build “members of group” table/page
    c.execute("SELECT username_full, nt_hash FROM hash_infos WHERE \"{}\" = 1 AND history_index = -1".format(group_name))
    member_rows = c.fetchall()
    num_groupmembers = len(member_rows)

    detailed_member_rows = []
    for username_full, nt_hash in member_rows:
        # Users sharing this hash
        c.execute("SELECT username_full FROM hash_infos WHERE nt_hash = \"%s\" AND history_index = -1" % nt_hash)
        users_rows = c.fetchall()
        share_cnt = len(users_rows)
        if share_cnt < 30:
            shared_users_str = ', '.join(''.join(u) for u in users_rows)
        else:
            shared_users_str = "Too Many to List"

        # Pull password + LM info
        c.execute("SELECT password, lm_hash FROM hash_infos WHERE nt_hash = \"%s\" AND history_index = -1 LIMIT 1" % nt_hash)
        pw, lm = c.fetchone()
        lm_present = "Yes" if lm != "aad3b435b51404eeaad3b435b51404ee" else "No"

        detailed_member_rows.append((username_full, nt_hash, shared_users_str, share_cnt, pw, lm_present))

    # Sanitize passwords and hashes in the data
    sanitized_member_rows = [sanitize_table_row(row, [4], [1]) for row in detailed_member_rows]  # password at index 4, nt_hash at index 1

    member_headers = ["Username", "NT Hash", "Users Sharing this Hash", "Share Count", "Password", "Non-Blank LM Hash?"]
    hbt_members = HtmlBuilder()
    hbt_members.add_table_to_html(sanitized_member_rows, member_headers)
    # Sanitize group name for filename
    safe_group_name = re.sub(r'[<>:"/\\|?*]', '_', group_name)
    members_filename = hbt_members.write_html_report(f"{safe_group_name}_members.html")

    # 2) Build “cracked passwords for group” table/page
    c.execute("""SELECT username_full, LENGTH(password) as plen, password, only_lm_cracked
                 FROM hash_infos
                 WHERE "{}" = 1 AND password is not NULL AND password != '' AND history_index = -1
                 ORDER BY plen""".format(group_name))
    cracked_rows = c.fetchall()
    num_groupmembers_cracked = len(cracked_rows)

    # Sanitize passwords in the data
    sanitized_cracked_rows = [sanitize_table_row(row, [2], []) for row in cracked_rows]  # password at index 2

    cracked_headers = [f'Username of "{group_name}" Member', "Password Length", "Password", "Only LM Cracked"]
    hbt_cracked = HtmlBuilder()
    hbt_cracked.add_table_to_html(sanitized_cracked_rows, cracked_headers)
    cracked_filename = hbt_cracked.write_html_report(f"{safe_group_name}_cracked_passwords.html")

    # 3) Add a single row for THIS GROUP to the groups summary page list
    percent_cracked = pct(num_groupmembers_cracked, num_groupmembers)

    group_summary_rows.append((
        group_name,
        num_groupmembers,
        num_groupmembers_cracked,
        f"{percent_cracked}%",                          # ← new value
        f'<a href="{members_filename}">Details</a>',
        f'<a href="{cracked_filename}">Details</a>'
    ))

# --- AFTER THE LOOP: WRITE GROUPS PAGE ---
hbt_groups = HtmlBuilder()
hbt_groups.add_table_to_html(
        group_summary_rows,
        headers=group_page_headers,
        cols_to_not_escape=(4, 5)          # ← keep anchor tags alive
)
groups_page_filename = hbt_groups.write_html_report("groups_stats.html")

# --- ADD ONE ROW TO THE MASTER SUMMARY TABLE ---
summary_table.append((
    None,
    None,
    "Group Cracking Statistics",
    f'<a href="{groups_page_filename}">Details</a>'
))

# ── Password‑policy length violations ─────────────────────────────────
c.execute('''
    SELECT username,
           LENGTH(password) AS plen,
           password
    FROM   hash_infos
    WHERE  history_index = -1
      AND  password IS NOT NULL
      AND  LENGTH(password) < ?
''', (min_len,))
violating_rows = c.fetchall()    # (username, plen, password)

if violating_rows:
    # Build HTML table: User | Actual Len | Policy Len | Password
    hbt_policy = HtmlBuilder()
    headers = ["Username", "Password Length", "Policy Min Length", "Password"]
    data = [(u, plen, min_len, ("" if p is None else p))
            for u, plen, p in violating_rows]
    # Sanitize passwords in the data
    sanitized_data = [sanitize_table_row(row, [3], []) for row in data]  # password at index 3
    hbt_policy.add_table_to_html(sanitized_data, headers, cols_to_not_escape=3)

    policy_filename = hbt_policy.write_html_report("password_policy_violations.html")

    # Add a line to summary_table → Count • Description • Details link
    summary_table.append((
        len(violating_rows), pct(len(violating_rows), num_hashes),
        f"Accounts With Passwords Shorter Than {min_len} Characters",
        f'<a href="{policy_filename}">Details</a>'
    ))
else:
    print(f"[+] No cracked passwords shorter than {min_len} characters.")

# Users whose password equals their username
try:
    print("[*] Checking for users whose password equals their username...")
    c.execute("""
        SELECT username, password, LENGTH(password) AS plen, nt_hash
        FROM hash_infos
        WHERE history_index = -1
          AND password IS NOT NULL
    """)
    rows = c.fetchall()

    offenders = []
    for username, password, plen, nt_hash in rows:
        if username and password and username == password:
            offenders.append((username, password, plen, nt_hash))

    # Build the HTML details table
    if offenders:
        print(f"[+] Found {len(offenders)} users with username == password")
        # Sanitize passwords and hashes in the data
        sanitized_offenders = [sanitize_table_row(row, [1], [3]) for row in offenders]  # password at index 1, nt_hash at index 3
        hbt_user_as_pass = HtmlBuilder()
        hbt_user_as_pass.add_table_to_html(
            sanitized_offenders,
            ["Username", "Password", "Password Length", "NT Hash"]
        )

        filename = hbt_user_as_pass.write_html_report("username_equals_password.html")
        summary_table.append((
            len(offenders),
            pct(len(offenders), num_hashes),
            "Accounts Using Username As Password",
            f'<a href="{filename}">Details</a>'
        ))
except Exception as e:
    print(f"[!] Error while checking username==password: {e!r}")

try:
    print("[*] Checking for users whose password equals their username by hash comparison...")
    c.execute("""
        SELECT username, 
               COALESCE(username_full, username) AS username_full,
               nt_hash,
               password
        FROM hash_infos
        WHERE history_index = -1
          AND nt_hash IS NOT NULL
          AND username IS NOT NULL
    """)
    rows = c.fetchall()

    # Track users already found by the "cracked password equals username" pass to avoid dupes
    already_flagged: set[str] = set()
    try:
        # Seed already_flagged with offenders from the previous pass
        already_flagged.update(o[0] for o in offenders)  # offenders: [(username, password, plen, nt_hash)]
    except NameError:
        pass

    offenders_hashed: list[tuple[str, str, int, str]] = []

    for username, username_full, nt_hash, cracked_pw in rows:
        # If the earlier cracked-check already flagged this user, skip
        if username in already_flagged:
            continue

        # Quick win: if we *do* have a cracked password, check equality (case-insensitive) one more time
        # in case the earlier pass used strict case. Optional — remove if you don’t want this.
        if cracked_pw:
            if cracked_pw == username or cracked_pw.lower() == username.lower():
                offenders_hashed.append((username_full or username, cracked_pw, len(cracked_pw), nt_hash))
                continue

        # Build username-based password candidates and compare by NT hash
        cands = username_candidates(username, username_full)
        try:
            target = nt_hash.lower()
        except Exception:
            target = nt_hash

        matched = False
        for cand in cands:
            try:
                h = ntlm_hash(cand)
            except RuntimeError as e:
                print(f"[!] NT hash backend unavailable: {e}")
                raise
            if h == target:
                offenders_hashed.append((username_full or username, cand, len(cand), nt_hash))
                matched = True
                break

        if not matched and __debug__:
            # Optional noisy debug
            pass

    if offenders_hashed:
        print(f"[+] Found {len(offenders_hashed)} additional users whose password == username (hash match)")
        # Sanitize passwords and hashes in the data
        sanitized_offenders_hashed = [sanitize_table_row(row, [1], [3]) for row in offenders_hashed]  # password at index 1, nt_hash at index 3
        hbt_user_as_pass_hash = HtmlBuilder()
        hbt_user_as_pass_hash.add_table_to_html(
            sanitized_offenders_hashed,
            ["Username", "Derived Password (from username)", "Password Length", "NT Hash"]
        )
        filename2 = hbt_user_as_pass_hash.write_html_report("username_equals_password_by_hash.html")
        summary_table.append((
            len(offenders_hashed),
            pct(len(offenders_hashed), num_hashes),
            "Accounts Using Username As Password Not Cracked (by hash)",
            f'<a href="{filename2}">Details</a>'
        ))
    else:
        print("[*] No additional username==password accounts found by hash comparison.")
except Exception as e:
    print(f"[!] Error while hash-checking username==password: {e!r}")

# Number of LM hashes in the NTDS file, excluding the blank value
c.execute('SELECT count(*) FROM hash_infos WHERE lm_hash is not "aad3b435b51404eeaad3b435b51404ee" AND history_index = -1')
num_lm_hashes = c.fetchone()[0]
percent_lm_hashes = pct(num_lm_hashes, num_hashes)
summary_table.append((num_lm_hashes, percent_lm_hashes, "LM Hashes (Non-blank)", None))

# Number of UNIQUE LM hashes in the NTDS, excluding the blank value
c.execute('SELECT count(DISTINCT lm_hash) FROM hash_infos WHERE lm_hash is not "aad3b435b51404eeaad3b435b51404ee" AND history_index = -1')
num_unique_lm_hashes = c.fetchone()[0]
percent_unique_lm_hashes = pct(num_unique_lm_hashes, num_hashes)
summary_table.append((num_unique_lm_hashes, percent_unique_lm_hashes, "Unique LM Hashes (Non-blank)", None))

# Number of passwords that are LM cracked for which you don't have the exact (case sensitive) password.
c.execute('SELECT lm_hash, lm_pass_left, lm_pass_right, nt_hash FROM hash_infos WHERE (lm_pass_left is not "" or lm_pass_right is not "") AND history_index = -1 and password is NULL and lm_hash is not "aad3b435b51404eeaad3b435b51404ee" group by lm_hash')
rows = c.fetchall()
num_lm_hashes_cracked_where_nt_hash_not_cracked = len(rows)
output = "<div class='text-left'>WARNING there were %d unique LM hashes for which you do not have the password." % num_lm_hashes_cracked_where_nt_hash_not_cracked
if num_lm_hashes_cracked_where_nt_hash_not_cracked != 0:
    hbt = HtmlBuilder()
    headers = ["LM Hash", "Left Portion of Password",
               "Right Portion of Password", "NT Hash"]
    # Sanitize passwords and hashes in the data
    sanitized_lm_rows = [sanitize_table_row(row, [1, 2], [0, 3]) for row in rows]  # passwords at indices 1,2; hashes at indices 0,3
    hbt.add_table_to_html(sanitized_lm_rows, headers)
    filename = hbt.write_html_report("lm_noncracked.html")
    output += ' <a href="' + filename + '">Details</a>'
    output += "</br></br>Cracking these to their 7-character upcased representation is easy with Hashcat and this tool will determine the correct case and concatenate the two halves of the password for you!</br></br> Try this Hashcat command to crack all LM hashes:</br> <strong>./hashcat64.bin -m 3000 -a 3 customer.ntds -1 ?a ?1?1?1?1?1?1?1 --increment</strong></br></br> Or for John, try this:</br> <strong>john --format=LM customer.ntds</strong></br>"
    hb.build_html_body_string(output)

# Count and List of passwords that were only able to be cracked because the LM hash was available, includes usernames
c.execute('SELECT username_full,password,LENGTH(password) as plen,only_lm_cracked FROM hash_infos WHERE only_lm_cracked = 1 ORDER BY plen AND history_index = -1')
rows = c.fetchall()
hbt = HtmlBuilder()
headers = ["Username", "Password", "Password Length", "Only LM Cracked"]
# Sanitize passwords in the data
sanitized_lm_cracked_rows = [sanitize_table_row(row, [1], []) for row in rows]  # password at index 1
hbt.add_table_to_html(sanitized_lm_cracked_rows, headers)
filename = hbt.write_html_report("users_only_cracked_through_lm.html")
percent_only_lm_cracked = pct(len(rows), num_hashes)
summary_table.append((len(rows), percent_only_lm_cracked, "Passwords Only Cracked via LM Hash",
                      "<a href=\"" + filename + "\">Details</a>"))
c.execute('SELECT COUNT(DISTINCT nt_hash) FROM hash_infos WHERE only_lm_cracked = 1 AND history_index = -1')
num_unique_lm_hashes_not_cracked = c.fetchone()[0]
percent_unique_lm_hashes_not_cracked = pct(num_unique_lm_hashes_not_cracked, num_hashes)
summary_table.append(
    (num_unique_lm_hashes_not_cracked, percent_unique_lm_hashes_not_cracked, 
     "Unique LM Hashes Cracked Where NT Hash Was Not Cracked", None))

# Password length statistics
c.execute('SELECT LENGTH(password) as plen,COUNT(password) FROM hash_infos WHERE plen is not NULL AND history_index = -1 AND plen <> 0 GROUP BY plen ORDER BY plen')
rows = c.fetchall()
counter = 0
for plen, count in rows:
    c.execute('SELECT username FROM hash_infos WHERE history_index = -1 AND LENGTH(password) = ?', (plen,))
    usernames = c.fetchall()
    hbt = HtmlBuilder()
    headers = ["Users with a password length of " + str(plen)]
    hbt.add_table_to_html(usernames, headers)
    filename = hbt.write_html_report(str(counter) + "length_usernames.html")
    rows[counter] += ("<a href=\"" + filename + "\">Details</a>",)
    counter += 1
hbt = HtmlBuilder()
headers = ["Password Length", "Count", "Details"]
hbt.add_table_to_html(rows, headers, 2)
c.execute('SELECT COUNT(password) as count, LENGTH(password) as plen FROM hash_infos WHERE plen is not NULL AND history_index = -1 and plen is not 0 GROUP BY plen ORDER BY count DESC')
rows = c.fetchall()
headers = ["Count", "Password Length"]
hbt.add_table_to_html(rows, headers)
filename = hbt.write_html_report("password_length_stats.html")
summary_table.append((None, None, "Password Length Stats",
                      "<a href=\"" + filename + "\">Details</a>"))

# Top Ten Passwords Used
c.execute('SELECT password,COUNT(password) as count FROM hash_infos WHERE password is not NULL AND history_index = -1 and password is not "" GROUP BY password ORDER BY count DESC LIMIT 20')
rows = c.fetchall()
hbt = HtmlBuilder()
headers = ["Password", "Count"]
# Sanitize passwords in the data
sanitized_top_passwords = [sanitize_table_row(row, [0], []) for row in rows]  # password at index 0
hbt.add_table_to_html(sanitized_top_passwords, headers)
filename = hbt.write_html_report("top_password_stats.html")
summary_table.append((None, None, "Top Password Use Stats",
                      "<a href=\"" + filename + "\">Details</a>"))

# Password Reuse Statistics (based only on NT hash)
c.execute('SELECT nt_hash, COUNT(nt_hash) as count, password FROM hash_infos WHERE nt_hash is not "31d6cfe0d16ae931b73c59d7e0c089c0" AND history_index = -1 GROUP BY nt_hash ORDER BY count DESC LIMIT 20')
rows = c.fetchall()
counter = 0
for idx, (nt_hash, hit_count, pwd) in enumerate(rows):
    c.execute(
        'SELECT username FROM hash_infos WHERE nt_hash = ? AND history_index = -1', (nt_hash,))
    usernames = c.fetchall()
    if pwd is None:
        pwd = ""
    hbt = HtmlBuilder()
    headers = ["Users Sharing a hash:password of " +
               sanitize(nt_hash) + ":" + sanitize(pwd)]
    hbt.add_table_to_html(usernames, headers)
    filename = hbt.write_html_report(str(counter) + "reuse_usernames.html")
    rows[counter] += ("<a href=\"" + filename + "\">Details</a>",)
    counter += 1
hbt = HtmlBuilder()
headers = ["NT Hash", "Count", "Password", "Details"]
# Sanitize passwords and hashes in the data
sanitized_reuse_rows = [sanitize_table_row(row, [2], [0]) for row in rows]  # password at index 2, nt_hash at index 0
hbt.add_table_to_html(sanitized_reuse_rows, headers, 3)
filename = hbt.write_html_report("password_reuse_stats.html")
summary_table.append((None, None, "Password Reuse Stats",
                      "<a href=\"" + filename + "\">Details</a>"))

# Password History Stats
c.execute('SELECT MAX(history_index) FROM hash_infos;')
max_password_history = c.fetchone()
max_password_history = max_password_history[0]
hbt = HtmlBuilder()
if max_password_history < 0:
    hbt.build_html_body_string("There was no history contained in the password files.  If you would like to get the password history, run secretsdump.py with the flag \"-history\". <br><br> Sample secretsdump.py command: secretsdump.py -system registry/SYSTEM -ntds \"Active Directory/ntds.dit\" LOCAL -outputfile customer -history")
else:
    password_history_headers = ["Username", "Current Password"]
    column_names = ["cp"]
    command = 'SELECT * FROM ( '
    command += 'SELECT history_base_username'
    for i in range(-1,max_password_history + 1):
        if i == -1:
            column_names.append("cp")
        else:
            password_history_headers.append("History " + str(i))
            column_names.append("h" + str(i))
        command += (', MIN(CASE WHEN history_index = ' + str(i) + ' THEN password END) ' + column_names[-1])
    command += (' FROM hash_infos GROUP BY history_base_username) ')
    command += "WHERE coalesce(" + ",".join(column_names) + ") is not NULL"
    c.execute(command)
    rows = c.fetchall()
    headers = password_history_headers
    hbt.add_table_to_html(rows, headers, 8)
filename=hbt.write_html_report("password_history.html")
summary_table.append((None, None, "Password History",
                "<a href=\"" + filename + "\">Details</a>"))

# Write out the main report page
hb.add_table_to_html(summary_table, summary_table_headers, 3)
hb.write_html_report(filename_for_html_report)
print("The Report has been written to the \"" + filename_for_html_report +
      "\" file in the \"" + folder_for_html_report + "\" directory")

# Save (commit) the changes and close the database connection
conn.commit()
conn.close()

try:
    input = raw_input
except NameError:
    pass

# prompt user to open the report
# the code to prompt user to open the file was borrowed from the EyeWitness tool https://github.com/ChrisTruncer/EyeWitness
print('Would you like to open the report now? [Y/n]')
while True:
    try:
        response = input().lower().rstrip('\r')
        if ((response == "") or (strtobool(response))):
            webbrowser.open(os.path.join("file://" + os.getcwd(),
                                         folder_for_html_report, filename_for_html_report))
            break
        else:
            break
    except ValueError:
        print("Please respond with y or n")
