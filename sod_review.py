"""Compatibility import path for teams that name the module ``sod_review``."""

from access_review import *  # noqa: F401,F403
from access_review import (  # noqa: F401
    APPROVE_DUTY,
    ENTER_DUTY,
    SodReviewer,
    review_batch,
    review_request,
)
