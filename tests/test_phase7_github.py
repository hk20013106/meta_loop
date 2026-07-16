from datetime import UTC, datetime

import pytest

from meta_loop.domain.errors import ValidationError
from meta_loop.infrastructure.github import GitHubIssueSource


REVISION = "b" * 40


def issue(number=17, pull_request=False):
    value = {
        "id": 17,
        "number": number,
        "state": "open",
        "body": "```meta-loop-json\n{\"schema_version\":1,\"objective\":\"Update\",\"acceptance_criteria\":[\"pass\"],\"revision\":\"%s\",\"risk\":\"R1\"}\n```" % REVISION,
    }
    if pull_request:
        value["pull_request"] = {"url": "ignored"}
    return value


class FakeTransport:
    def __init__(self, actor="Kai"):
        self.actor = actor
        self.paths = []

    def get_json(self, path):
        self.paths.append(path)
        if path.startswith("/repos/owner/repository/issues?"):
            return [issue(), issue(18, pull_request=True)]
        if path == "/repos/owner/repository/issues/17/events":
            return [{"id": 91, "event": "labeled", "label": {"name": "meta-loop:ready"}, "actor": {"login": self.actor}, "created_at": "2026-07-16T00:00:00Z"}]
        if path == "/repos/owner/repository/commits/" + REVISION:
            return {"sha": REVISION}
        raise AssertionError(path)


def test_github_source_uses_api_label_actor_and_excludes_pull_requests():
    transport = FakeTransport()
    source = GitHubIssueSource("owner/repository", "Kai", "meta-loop:ready", transport)
    values = source.scan(10)
    assert len(values) == 1
    assert values[0].source_reference == "owner/repository#17"
    assert values[0].trigger_event_id == "91"
    assert values[0].occurred_at == datetime(2026, 7, 16, tzinfo=UTC)
    assert all("token" not in path.lower() for path in transport.paths)


def test_github_source_fails_closed_for_an_unauthorized_label_actor():
    with pytest.raises(ValidationError, match="authorized"):
        GitHubIssueSource("owner/repository", "Kai", "meta-loop:ready", FakeTransport("other")).scan(10)
