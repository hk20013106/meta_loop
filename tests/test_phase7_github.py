from datetime import UTC, datetime

import pytest

from meta_loop.domain.errors import ValidationError
from meta_loop.infrastructure.github import GitHubIssueSource, MAX_RESPONSE_BYTES, UrllibJsonTransport


REVISION = "b" * 40


def issue(number=17, pull_request=False):
    value = {
        "id": 17,
        "number": number,
        "state": "open",
        "labels": [{"name": "meta-loop:ready"}],
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
        if path == "/repos/owner/repository/issues/17":
            return issue()
        if path.startswith("/repos/owner/repository/issues/17/events?"):
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


def test_github_source_refetches_and_rejects_state_or_label_drift():
    class DriftingTransport(FakeTransport):
        def get_json(self, path):
            if path == "/repos/owner/repository/issues/17":
                changed = issue()
                changed["state"] = "closed"
                return changed
            return super().get_json(path)
    with pytest.raises(ValidationError, match="open"):
        GitHubIssueSource("owner/repository", "Kai", "meta-loop:ready", DriftingTransport()).scan(10)


def test_github_source_rejects_boolean_identifiers_in_the_refetched_issue():
    class BooleanRefetchTransport(FakeTransport):
        def get_json(self, path):
            if path == "/repos/owner/repository/issues/17":
                changed = issue()
                changed["id"] = True
                return changed
            return super().get_json(path)
    with pytest.raises(ValidationError, match="payload"):
        GitHubIssueSource("owner/repository", "Kai", "meta-loop:ready", BooleanRefetchTransport()).scan(10)


@pytest.mark.parametrize("repository, actor, label", [
    ("owner/repo/extra", "Kai", "meta-loop:ready"),
    ("owner/repo", "bad\nactor", "meta-loop:ready"),
    ("owner/repo", "Kai", "bad\rlabel"),
])
def test_github_source_rejects_unsafe_configuration(repository, actor, label):
    with pytest.raises(ValidationError):
        GitHubIssueSource(repository, actor, label, FakeTransport())


def test_github_source_uses_the_last_matching_event_across_bounded_pages():
    class PagedTransport(FakeTransport):
        def get_json(self, path):
            if path.endswith("events?per_page=100&page=1"):
                return [{"id": index, "event": "labeled", "label": {"name": "meta-loop:ready"}, "actor": {"login": "Kai"}, "created_at": "2026-07-16T00:00:00Z"} for index in range(100)]
            if path.endswith("events?per_page=100&page=2"):
                return [{"id": 101, "event": "labeled", "label": {"name": "meta-loop:ready"}, "actor": {"login": "Kai"}, "created_at": "2026-07-17T00:00:00Z"}]
            return super().get_json(path)
    assert GitHubIssueSource("owner/repository", "Kai", "meta-loop:ready", PagedTransport()).scan(10)[0].trigger_event_id == "101"


def test_same_second_numeric_event_order_selects_later_unauthorized_actor():
    class SameSecondTransport(FakeTransport):
        def get_json(self, path):
            if path.startswith("/repos/owner/repository/issues/17/events?"):
                return [
                    {"id": 9, "event": "labeled", "label": {"name": "meta-loop:ready"}, "actor": {"login": "Kai"}, "created_at": "2026-07-16T00:00:00Z"},
                    {"id": 10, "event": "labeled", "label": {"name": "meta-loop:ready"}, "actor": {"login": "other"}, "created_at": "2026-07-16T00:00:00Z"},
                ]
            return super().get_json(path)
    with pytest.raises(ValidationError, match="authorized"):
        GitHubIssueSource("owner/repository", "Kai", "meta-loop:ready", SameSecondTransport()).scan(10)


def test_github_source_rejects_boolean_scan_limit():
    with pytest.raises(ValidationError, match="scan limit"):
        GitHubIssueSource("owner/repository", "Kai", "meta-loop:ready", FakeTransport()).scan(True)


def test_github_source_rejects_naive_event_timestamps():
    with pytest.raises(ValidationError, match="time"):
        GitHubIssueSource._parse_time("2026-07-16T00:00:00")


def test_github_source_rejects_body_or_revision_drift_after_refetch():
    class DriftTransport(FakeTransport):
        def get_json(self, path):
            if path == "/repos/owner/repository/issues/17":
                changed = issue()
                changed["body"] = changed["body"].replace(REVISION, "c" * 40)
                return changed
            return super().get_json(path)
    with pytest.raises(ValidationError, match="drift"):
        GitHubIssueSource("owner/repository", "Kai", "meta-loop:ready", DriftTransport()).scan(10)


def test_urllib_transport_rejects_oversized_response_before_json_parse():
    class Response:
        def read(self, limit):
            assert limit == MAX_RESPONSE_BYTES + 1
            return b"x" * limit
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
    with pytest.raises(ValidationError, match="too large"):
        UrllibJsonTransport("token", lambda request, timeout: Response()).get_json("/repos/o/r/issues")
