"""Whitelisted response mapping for external APIs."""
import re

_ERROR_CODES = {
    "kaggle kernels push failed": "KAGGLE_SUBMISSION_FAILED",
    "timed out waiting for the kaggle batch": "KAGGLE_RUN_TIMEOUT",
    "submission did not complete": "KAGGLE_SUBMISSION_TIMEOUT",
    "kaggle reported an error": "KAGGLE_REPORTED_FAILURE",
    "unexpected launcher error": "LAUNCHER_ERROR",
}
_ERROR_CODE_RE = re.compile(r"^[A-Z0-9_]{3,64}$")


def error_code(error: str | None) -> str | None:
    if not error:
        return None
    if _ERROR_CODE_RE.match(error):
        return error
    lowered = error.lower()
    for needle, code in _ERROR_CODES.items():
        if needle in lowered:
            return code
    return "UNKNOWN_ERROR"
