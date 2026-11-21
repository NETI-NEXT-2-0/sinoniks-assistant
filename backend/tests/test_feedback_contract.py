from uuid import uuid4

import pytest
from pydantic import ValidationError

from partner_agent.feedback_routes import Submission


@pytest.mark.parametrize("comment", ["bad\x00text", "bad\ud800text", "x" * 2001])
def test_invalid_comment_cannot_reach_postgres(comment):
    with pytest.raises(ValidationError):
        Submission(id=uuid4(), run_id=uuid4(), rating=3, comment=comment)


def test_empty_comment_is_a_valid_rating_only_submission():
    assert Submission(id=uuid4(), run_id=uuid4(), rating=5).comment == ""
