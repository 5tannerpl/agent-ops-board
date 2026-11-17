"""Whitelisted response mapping for external APIs."""

_ERROR_CODES = {
    "kaggle kernels push failed": "KAGGLE_SUBMISSION_FAILED",
    "timed out waiting for the kaggle batch": "KAGGLE_RUN_TIMEOUT",
    "submission did not complete": "KAGGLE_SUBMISSION_TIMEOUT",
    "kaggle reported an error": "KAGGLE_REPORTED_FAILURE",
    "unexpected launcher error": "LAUNCHER_ERROR",
}

_ALLOWED_ERROR_CODES = set(_ERROR_CODES.values()) | {
    "UNKNOWN_ERROR",
}


def error_code(error: str | None) -> str | None:
    if not error:
        return None

    if error in _ALLOWED_ERROR_CODES:
        return error

    lowered = error.lower()

    for needle, code in _ERROR_CODES.items():
        if needle in lowered:
            return code

    return "UNKNOWN_ERROR"
