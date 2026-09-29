"""The test table from the exercise's docs/tool-contracts.md, plus contract details."""

from datetime import datetime, timezone

from triage.corpus import split_sections
from triage.tools import effective_at, search_changes, search_logs, search_runbook


def refs(result):
    return [(r["file"], r["line"]) for r in result["results"]]


def test_logs_pool(data_dir):
    r = search_logs(data_dir, "pool")
    assert r["total_matches"] == 19 and not r["truncated"]
    files = [f for f, _ in refs(r)]
    assert files.count("checkout-service.log") > len(files) / 2


def test_logs_sorted_and_truncated(data_dir):
    r = search_logs(data_dir, "pool", max_results=5)
    assert (r["returned"], r["truncated"], r["total_matches"]) == (5, True, 19)
    full = search_logs(data_dir, "pool")["results"]
    stamps = [x["text"].split()[0] for x in full]
    assert stamps == sorted(stamps)


def test_logs_checkout_deploy(data_dir):
    files = {f for f, _ in refs(search_logs(data_dir, "checkout deploy"))}
    assert "platform-events.log" in files


def test_every_term_must_match(data_dir):
    """AND semantics across all three tools: a multi-term query never matches more than its
    rarest term alone, and every hit contains every term."""
    both = search_logs(data_dir, "checkout deploy")
    assert both["total_matches"] < min(search_logs(data_dir, t)["total_matches"] for t in ("checkout", "deploy"))
    assert all("checkout" in r["text"].lower() and "deploy" in r["text"].lower() for r in both["results"])
    for tool, terms in ((search_changes, ("checkout", "rollback")), (search_runbook, ("connection", "pool"))):
        n_both = tool(data_dir, " ".join(terms), max_results=50)["total_matches"]
        assert n_both <= min(tool(data_dir, t, max_results=50)["total_matches"] for t in terms)
        assert n_both < max(tool(data_dir, t, max_results=50)["total_matches"] for t in terms)


def test_logs_line_numbers_are_one_based(data_dir):
    hit = search_logs(data_dir, "pool")["results"][0]
    lines = (data_dir / "logs" / hit["file"]).read_text().splitlines()
    assert lines[hit["line"] - 1] == hit["text"]


def test_logs_zero_and_empty(data_dir):
    assert search_logs(data_dir, "zzz") == {
        "query": "zzz", "total_matches": 0, "returned": 0, "truncated": False, "results": []}
    for tool in (search_logs, search_changes, search_runbook):
        assert tool(data_dir, "   ") == {"error": "query must contain at least one term"}


def test_logs_case_insensitive(data_dir):
    assert search_logs(data_dir, "POOL")["total_matches"] == 19


def test_changes_pool_max(data_dir):
    r = search_changes(data_dir, "db.pool.max")
    assert r["total_matches"] == 2


def test_changes_checkout_newest_first(data_dir):
    r = search_changes(data_dir, "checkout")
    assert r["total_matches"] == 4
    dates = [effective_at(x["text"]) for x in r["results"]]
    assert dates == sorted(dates, reverse=True)


def test_changes_full_record(data_dir):
    rec = search_changes(data_dir, "checkout")["results"][0]
    assert set(rec) == {"id", "title", "text"}
    assert "**" in rec["text"] and not rec["text"].rstrip().endswith("---")


def test_effective_at_precedence_and_trailing_text():
    body = ("- **Merged:** 2026-08-12 11:03 UTC\n"
            "- **Shipped:** 2026-08-12 19:31 UTC in checkout-service v4.12.0\n")
    assert effective_at(body) == datetime(2026, 8, 12, 19, 31, tzinfo=timezone.utc)
    assert effective_at("- **Applied:** 2026-07-30 14:00 UTC") is not None
    assert effective_at("no dates here") is None


def test_every_change_record_has_a_date(data_dir):
    records = split_sections((data_dir / "changes" / "change-log.md").read_text())
    assert records and all(effective_at(r.body) for r in records)


def test_runbook_connection_pool(data_dir):
    r = search_runbook(data_dir, "connection pool")
    sources = {x["source"] for x in r["results"]}
    assert len(sources) >= 2 and any(s.startswith("_archive/") for s in sources)
    assert all("status" not in x for x in r["results"])


def test_runbook_timeout(data_dir):
    assert search_runbook(data_dir, "timeout")["total_matches"] > 1


def test_runbook_noisy_neighbour(data_dir):
    r = search_runbook(data_dir, "noisy neighbour")
    assert r["total_matches"] == 0 and r["results"] == []


def test_runbook_ordered_by_id(data_dir):
    ids = [x["id"] for x in search_runbook(data_dir, "the", max_results=50)["results"]]
    assert ids == sorted(ids)


def test_runbook_index_contributes_nothing(data_dir):
    r = search_runbook(data_dir, "owner", max_results=50)
    assert all(x["source"] != "README.md" for x in r["results"])
