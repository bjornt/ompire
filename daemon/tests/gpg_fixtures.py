"""Real GnuPG output, captured verbatim, for the signing-key probe tests.

Every string here was produced by GnuPG 2.4.8 against a throwaway keyring
rather than written by hand, so the field positions the parser depends on are
observed rather than assumed.  The revoked record is the one exception and is
marked as synthetic.

The keyring these came from holds:

- a certify-only primary (``cSC``) with a passphrase-protected signing subkey
  (``s``) — the shape the earlier probe expected;
- a passphrase-less primary that signs directly (``scSC``) and has no subkey —
  the shape the earlier probe could not find and misreported as locked;
- an expired signing key (validity ``e``) that must never be a candidate.
"""

from __future__ import annotations

# --- keys present in the captured listings ---------------------------------

PROTECTED_FPR = "2188462AA5F78D68B61D6D5E865639DBB930B899"
PROTECTED_KEYGRIP = "64FA6AD68C1C1FBF839F77E9719B0C0EA0F60070"
PROTECTED_UID = "Protected Key <prot@example.com>"
PROTECTED_KEY_ID = "865639DBB930B899"
PROTECTED_PRIMARY_FPR = "0E7D7E1B70D1C5419E35252302EB9B75E09B901F"
PROTECTED_PRIMARY_KEY_ID = "02EB9B75E09B901F"

UNPROTECTED_FPR = "B8100B4B05105E645A095BDA281A1010030F8451"
UNPROTECTED_KEYGRIP = "4BE52F04B36A6E2CF24A016CFEDFE99D9B974536"
UNPROTECTED_UID = "Unprotected Key <unprot@example.com>"
UNPROTECTED_KEY_ID = "281A1010030F8451"

EXPIRED_FPR = "8468933FE19B42DDAB635DE14AB98E877C4B5301"

# --- `gpg --list-secret-keys --with-colons --with-keygrip` ------------------

# The certify-only primary plus its protected signing subkey.
PROTECTED_ONLY = """\
sec:u:255:22:02EB9B75E09B901F:1787929473:::u:::cSC:::+::ed25519:::0:
fpr:::::::::0E7D7E1B70D1C5419E35252302EB9B75E09B901F:
grp:::::::::C86154BAB9EA1E5E6FD37B86B5BDDB1C92AD56C6:
uid:u::::1787929473::3C83B38DC222E069DED5774D7DD5C867F38E9D65::\
Protected Key <prot@example.com>::::::::::0:
ssb:u:255:22:865639DBB930B899:1787929475::::::s:::+::ed25519::
fpr:::::::::2188462AA5F78D68B61D6D5E865639DBB930B899:
grp:::::::::64FA6AD68C1C1FBF839F77E9719B0C0EA0F60070:
"""

# The passphrase-less primary that signs without a subkey.
UNPROTECTED_ONLY = """\
sec:u:255:22:281A1010030F8451:1787929477:::u:::scSC:::+::ed25519:::0:
fpr:::::::::B8100B4B05105E645A095BDA281A1010030F8451:
grp:::::::::4BE52F04B36A6E2CF24A016CFEDFE99D9B974536:
uid:u::::1787929477::976A76430AEA9460348AEAB9FCFF83DEBE1BB3BC::\
Unprotected Key <unprot@example.com>::::::::::0:
"""

# An expired signing key: validity `e`, expiry in field 7.
EXPIRED_ONLY = """\
sec:e:255:22:4AB98E877C4B5301:1787929515:1787929516::u:::sc:::+::ed25519:::0:
fpr:::::::::8468933FE19B42DDAB635DE14AB98E877C4B5301:
grp:::::::::2AA40D02835B3C455BC35C487EBAC9E3E5221176:
uid:e::::1787929515::5BDC1E210E5B8323B28EA0E95DCB789780B7DFB8::\
Expired Key <exp@example.com>::::::::::0:
"""

# Two usable keys and one unusable one — the ambiguous keyring.
TWO_USABLE = PROTECTED_ONLY + UNPROTECTED_ONLY + EXPIRED_ONLY

EMPTY = ""

# Synthetic: same shape as the captured records with validity `r`. GnuPG's
# non-interactive revocation path could not be driven in the capture session,
# so this record is hand-derived from the expired one.
REVOKED_ONLY = """\
sec:r:255:22:0C1AFAABCA2B22CB:1787929517:::u:::scSC:::+::ed25519:::0:
fpr:::::::::AF49BECD2F1B05CF7F9F725B0C1AFAABCA2B22CB:
grp:::::::::13620BA13E212E4F9938E81A7F70E12F19325D1B:
uid:r::::1787929517::50D628BACB0EC87707C1179283FAEAC92ED42F53::\
Revoked Key <rev@example.com>::::::::::0:
"""

# A primary whose secret half is a stub (field 15 `#`): nothing can sign.
STUB_SECRET_ONLY = """\
sec:u:255:22:0C1AFAABCA2B22CB:1787929517:::u:::scSC:::#::ed25519:::0:
fpr:::::::::AF49BECD2F1B05CF7F9F725B0C1AFAABCA2B22CB:
grp:::::::::13620BA13E212E4F9938E81A7F70E12F19325D1B:
uid:u::::1787929517::50D628BACB0EC87707C1179283FAEAC92ED42F53::\
Stub Key <stub@example.com>::::::::::0:
"""


# --- `gpg-connect-agent KEYINFO --no-ask <keygrip> /bye` --------------------
#
# Field order: S KEYINFO <grp> <type> <serialno> <idstr> <cached> <protection>
#              <fpr> <ttl> <flags>


def keyinfo(keygrip: str, *, cached: bool, protection: str, ttl: str = "-") -> str:
    """Build a KEYINFO response in the captured shape."""
    flag = "1" if cached else "-"
    return f"S KEYINFO {keygrip} D - - {flag} {protection} - {ttl} -\nOK\n"


# Captured verbatim.
PROTECTED_COLD = keyinfo(PROTECTED_KEYGRIP, cached=False, protection="P")
PROTECTED_WARM = keyinfo(PROTECTED_KEYGRIP, cached=True, protection="P")
UNPROTECTED_COLD = keyinfo(UNPROTECTED_KEYGRIP, cached=False, protection="C")

# Captured verbatim: the agent answers, but does not know the keygrip.
KEYINFO_NOT_FOUND = "ERR 67108891 Not found <GPG Agent>\nOK\n"

# Captured verbatim on stderr, with empty stdout, when the agent cannot start.
AGENT_DOWN_STDERR = (
    "gpg-connect-agent: no running gpg-agent - starting '/usr/bin/gpg-agent'\n"
    "gpg-connect-agent: can't connect to the gpg-agent: No such file or directory\n"
    "gpg-connect-agent: error sending standard options: No agent running\n"
)
