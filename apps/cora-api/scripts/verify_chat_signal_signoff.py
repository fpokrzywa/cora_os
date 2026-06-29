"""Deterministic verification of email sign-off normalization for SIGNAL chat drafts.

Covers the bug where a SIGNAL email draft shipped signed with the internal agent
codename ("Best regards, SIGNAL") because the draft body is the model reply verbatim
(body = response). Pure functions — NO DB, NO model call — CI/offline safe.

    docker cp apps/cora-api/scripts/verify_chat_signal_signoff.py cora-api:/tmp/vs.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vs.py     # 0=PASS 1=FAIL
"""
import sys

from app.routers.chat import CORA_SYSTEM_PROMPT, _extract_signal_fields
from app.signal_tools import normalize_email_signoff as _normalize_email_signoff

NAME = "Frank Pokrzywa"
FALLBACK = "Cora - the AI Assistant"


def main() -> int:
    fails: list[str] = []

    def expect(cond: bool, msg: str) -> None:
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # A) the reported bug: an agent codename sign-off is replaced with the user's name.
    out = _normalize_email_signoff(
        "Hi Freddie,\n\nLet's sync at 3pm.\n\nBest regards,\nSIGNAL", NAME)
    expect("SIGNAL" not in out, "agent codename 'SIGNAL' is stripped from the sign-off")
    expect(out.endswith("Best regards,\nFrank Pokrzywa"),
           "sign-off becomes the user's name under the same closing")

    # B) a multi-line signatory block collapses to the single canonical name.
    out2 = _normalize_email_signoff("Body here.\n\nRegards,\nSIGNAL\nThe AI Assistant", NAME)
    expect(out2.endswith("Regards,\nFrank Pokrzywa") and "The AI Assistant" not in out2,
           "a multi-line signatory block is replaced by the single canonical name")

    # C) no closing -> a clean one is appended (fallback name shown here).
    out3 = _normalize_email_signoff("Just the body, no closing.", FALLBACK)
    expect(out3.endswith(f"Best regards,\n{FALLBACK}"),
           "a body with no closing gets a clean appended sign-off")

    # D) a mid-sentence 'best' is NOT mistaken for a closing.
    out4 = _normalize_email_signoff("The best approach is to meet.\n\nBest,\nSIGNAL", NAME)
    expect("The best approach is to meet." in out4
           and out4.endswith("Best,\nFrank Pokrzywa") and "SIGNAL" not in out4,
           "a mid-sentence 'best' is preserved; only the closing is normalized")

    # E) idempotent.
    expect(_normalize_email_signoff(out, NAME) == out, "normalization is idempotent")

    # F) empty body stays empty (no spurious sign-off).
    expect(_normalize_email_signoff("", NAME) == "", "empty body -> empty")

    # G) _extract_signal_fields routes the draft body through normalization.
    fields = _extract_signal_fields(
        "email freddie", "Subject: Sync\n\nHi.\n\nThanks,\nSIGNAL", NAME)
    expect("SIGNAL" not in fields["body"]
           and fields["body"].endswith("Thanks,\nFrank Pokrzywa"),
           "_extract_signal_fields normalizes the draft body's sign-off")

    # H) the prompt guard is in place (belt to the deterministic suspenders).
    expect("do NOT sign it with an internal" in CORA_SYSTEM_PROMPT,
           "system prompt tells the model not to sign emails with an agent name")

    print()
    if fails:
        print(f"FAIL ({len(fails)}): " + "; ".join(fails))
        return 1
    print("PASS: signal sign-off normalization verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
