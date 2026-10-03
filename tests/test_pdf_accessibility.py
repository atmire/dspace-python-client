"""Tests for examples/pdf_accessibility: policy, analysis, advice, dates, pairing, sinks, state, engine."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
if str(_EXAMPLES) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES))

from pdf_accessibility.advice import Advice, format_pages, render_report
from pdf_accessibility.analysis import OCR_RULE, Analysis, analyse
from pdf_accessibility.eligibility import eligibility_basis, latest_date, parse_cutoff
from pdf_accessibility.pairing import File, normalize_name, pair_files
from pdf_accessibility.policy import Policy, PolicyError, RuleInfo, load_policy
from pdf_accessibility.scope import ScopedItem, pdf_bitstreams
from pdf_accessibility.sinks import marker, plan_metadata_patch, split_field, stale_markers
from pdf_accessibility.state import ResultStore, host_slug

FIELD = "local.accessibility.report"
U1 = "11111111-1111-1111-1111-111111111111"
U2 = "22222222-2222-2222-2222-222222222222"


def rule(
    rule_id="FIG-001", layer="conformance", severity="error", wcag=("1.1.1",), matterhorn="13"
):
    return RuleInfo(
        id=rule_id, title=rule_id, layer=layer, severity=severity, matterhorn=matterhorn, wcag=wcag
    )


CATALOGUE = {
    "FIG-001": rule("FIG-001"),
    "DOC-002": rule("DOC-002", wcag=("1.3.1",), matterhorn="01"),
    "DOC-012": rule("DOC-012", wcag=(), matterhorn="06"),
    "FIG-010": rule("FIG-010", layer="semantic", severity="warning"),
    "TAG-002": rule(
        "TAG-002", layer="semantic", severity="error", wcag=("1.3.1",), matterhorn="01"
    ),
}


# ------------------------------------------------------------------ policy


def write_policy(tmp_path, text):
    path = tmp_path / "p.yml"
    path.write_text(text)
    return load_policy(path)


def test_default_policy_loads():
    policy = load_policy()
    assert policy.standard == "wcag21-aa"
    assert policy.layers == {"semantic": "warning"}
    assert len(policy.hash) == 64


def test_rules_without_wcag_drop_to_info():
    assert load_policy().effective_severity(CATALOGUE["DOC-012"]) == "info"


def test_semantic_rules_capped_at_warning():
    assert load_policy().effective_severity(CATALOGUE["TAG-002"]) == "warning"


def test_wcag22_only_criteria_do_not_count_under_21():
    target_size = rule("X-1", wcag=("2.5.8",))
    assert Policy(standard="wcag21-aa").effective_severity(target_size) == "info"
    assert Policy(standard="wcag22-aa").effective_severity(target_size) == "error"


def test_pdfua1_keeps_engine_severity():
    assert Policy(standard="pdfua1").effective_severity(CATALOGUE["DOC-012"]) == "error"


def test_rule_and_matterhorn_overrides(tmp_path):
    policy = write_policy(tmp_path, "matterhorn:\n  13: warning\nrules:\n  FIG-001: ignore\n")
    assert policy.matterhorn == {"13": "warning"}
    assert policy.effective_severity(CATALOGUE["FIG-001"]) == "ignore"  # rule beats checkpoint
    assert policy.effective_severity(rule("FIG-002")) == "warning"


def test_policy_hash_changes_with_content(tmp_path):
    a = write_policy(tmp_path, "rules:\n  FIG-001: warning\n")
    b = write_policy(tmp_path, "rules:\n  FIG-001: error\n")
    assert a.hash != b.hash


@pytest.mark.parametrize(
    "text",
    ["standard: wcag3\n", "rules:\n  FIG-001: severe\n", "layers:\n  visual: warning\n", "- a\n"],
)
def test_invalid_policies_are_rejected(tmp_path, text):
    with pytest.raises(PolicyError):
        write_policy(tmp_path, text)


# ---------------------------------------------------------------- analysis


def raw_result(findings=(), ocr_pages=(), pages=4, crashed=(), error=None, criteria=()):
    return {
        "error": error,
        "stats": {"pages": pages},
        "findings": list(findings),
        "crashed_rules": list(crashed),
        "criteria": {"wcag22": list(criteria)},
        "ocr": {"pages": list(ocr_pages), "page_count": pages},
        "engine": {"outloud": "0.2.0"},
    }


def test_verdicts():
    policy = load_policy()
    fig = {"rule": "FIG-001", "severity": "error", "message": "m", "page": 2, "count": 1}
    sem = {"rule": "FIG-010", "severity": "warning", "message": "m"}
    pdfua = {"rule": "DOC-012", "severity": "error", "message": "m"}
    assert analyse(raw_result([fig]), policy, CATALOGUE).verdict == "FAIL"
    assert analyse(raw_result([sem]), policy, CATALOGUE).verdict == "REVIEW"
    assert analyse(raw_result([pdfua]), policy, CATALOGUE).verdict == "PASS"
    assert (
        analyse(raw_result([pdfua], crashed=["TBL-001"]), policy, CATALOGUE).verdict == "INCOMPLETE"
    )
    assert analyse(raw_result([fig], crashed=["TBL-001"]), policy, CATALOGUE).verdict == "FAIL"
    assert (
        analyse(raw_result(error="PdfError: broken", pages=0), policy, CATALOGUE).verdict
        == "UNREADABLE"
    )


def test_outloud_stopping_part_way_is_not_unreadable():
    policy = load_policy()
    pdfua = {"rule": "DOC-012", "severity": "error", "message": "m"}
    fig = {"rule": "FIG-001", "severity": "error", "message": "m", "page": 1}
    partial = analyse(
        raw_result([pdfua], error="TypeError: Object is not numeric"), policy, CATALOGUE
    )
    assert partial.verdict == "INCOMPLETE"
    assert partial.crashed_rules == ["outloud stopped part-way: TypeError: Object is not numeric"]
    failing = analyse(raw_result([fig], error="TypeError: x"), policy, CATALOGUE)
    assert failing.verdict == "FAIL"


def test_pages_come_from_boxes_and_page():
    finding = {
        "rule": "FIG-001",
        "severity": "error",
        "message": "2 figures",
        "page": 1,
        "count": 2,
        "boxes": [{"page": 1}, {"page": 3}],
    }
    analysis = analyse(raw_result([finding]), load_policy(), CATALOGUE)
    assert analysis.findings[0].pages == [1, 3]


def test_scanned_file_puts_ocr_first_and_prefixes_tagging_advice():
    doc002 = {"rule": "DOC-002", "severity": "error", "message": "no tree"}
    analysis = analyse(raw_result([doc002], ocr_pages=[1, 2, 3], pages=4), load_policy(), CATALOGUE)
    assert analysis.mostly_scanned
    assert [f.rule for f in analysis.findings] == ["OCR-001", "DOC-002"]
    assert analysis.findings[0].pages == [1, 2, 3]
    assert analysis.findings[1].fix.startswith("First run OCR")


def test_manual_criteria_respect_the_standard():
    criteria = [
        {"id": "1.4.3", "status": "manual"},
        {"id": "2.5.8", "status": "manual"},
        {"id": "1.1.1", "status": "pass"},
    ]
    analysis = analyse(raw_result(criteria=criteria), load_policy(), CATALOGUE)
    assert analysis.manual_criteria == ["1.4.3"]


def test_analysis_round_trips_through_dict():
    fig = {"rule": "FIG-001", "severity": "error", "message": "m", "page": 2}
    analysis = analyse(raw_result([fig]), load_policy(), CATALOGUE)
    again = Analysis.from_dict(json.loads(json.dumps(analysis.to_dict())))
    assert again == analysis


# ------------------------------------------------------------------ advice


@pytest.mark.parametrize(
    ("pages", "text"),
    [
        ([], "whole document"),
        ([4], "page 4"),
        ([1, 2, 3, 7, 9, 10], "pages 1-3, 7, 9-10"),
    ],
)
def test_format_pages(pages, text):
    assert format_pages(pages) == text


def test_format_pages_is_capped():
    text = format_pages(list(range(1, 60, 2)))
    assert "(30 pages in total)" in text


def test_render_report_uses_overrides_and_keeps_ocr_prefix():
    policy = load_policy()
    doc002 = {"rule": "DOC-002", "severity": "error", "message": "no tree"}
    analysis = analyse(raw_result([doc002], ocr_pages=[1, 2, 3], pages=3), policy, CATALOGUE)
    overrides = {
        "DOC-002": Advice(fix="Re-export with tags.", who="author"),
        "OCR-001": Advice(fix="Run OCR.", who=None),
    }
    text = render_report(analysis, policy, overrides, "2026-10-03")
    assert text.startswith("FAIL: 2 error(s)")
    assert "Fix: Run OCR." in text
    assert "Fix: First run OCR (see OCR-001). Then: Re-export with tags." in text
    assert "Who: author" in text
    assert text.index(OCR_RULE.id) < text.index("DOC-002")


def test_render_pass_says_what_pass_means():
    text = render_report(
        analyse(raw_result(), load_policy(), CATALOGUE), load_policy(), {}, "2026-10-03"
    )
    assert text.startswith("PASS")
    assert "does not make the file fully accessible" in text


# --------------------------------------------------------------- eligibility


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2027", date(2027, 12, 31)),
        ("2027-04", date(2027, 4, 30)),
        ("2024-02", date(2024, 2, 29)),
        ("2027-04-25", date(2027, 4, 25)),
        ("2026-10-03T11:51:46Z", date(2026, 10, 3)),
        ("2026-10-03T23:30:00-02:00", date(2026, 10, 4)),
        ("2027-13", None),
        ("2023-02-30", None),
        ("May 2019", None),
        ("", None),
    ],
)
def test_latest_date(value, expected):
    assert latest_date(value) == expected


def test_issued_wins_and_invalid_issued_falls_back_to_accessioned():
    md = {
        "dc.date.issued": [{"value": "2019"}],
        "dc.date.accessioned": [{"value": "2026-01-01T00:00:00Z"}],
    }
    assert eligibility_basis(md).field == "dc.date.issued"
    md["dc.date.issued"] = [{"value": "unknown"}]
    basis = eligibility_basis(md)
    assert (basis.field, basis.date) == ("dc.date.accessioned", date(2026, 1, 1))
    assert eligibility_basis({}) is None


def test_parse_cutoff():
    assert parse_cutoff("2027-04-26") == date(2027, 4, 26)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        parse_cutoff("2027")


# ----------------------------------------------------------------- pairing


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Thesis_Accessible.pdf", "thesis"),
        ("thesis-remediated-v2.pdf", "thesis"),
        ("thesis (1).pdf", "thesis"),
        ("nicaragua.pdf", "nicaragua"),
        ("report_ua.pdf", "report"),
    ],
)
def test_normalize_name(name, expected):
    assert normalize_name(name) == expected


def test_pairs_by_name_one_to_one():
    a, b = File("o1", "chapter1.pdf"), File("o2", "chapter2.pdf")
    a2 = File("c1", "chapter1_accessible.pdf")
    result = pair_files([a, b], [a2])
    assert result.pairs == [(a, a2)]
    assert result.unpaired_originals == [b]


def test_single_original_and_candidate_pair_even_with_different_names():
    o, c = File("o", "scan.pdf"), File("c", "final version.pdf")
    assert pair_files([o], [c]).pairs == [(o, c)]


def test_ambiguous_names_are_not_guessed():
    o1, o2 = File("o1", "a.pdf"), File("o2", "b.pdf")
    c = File("c", "new.pdf")
    result = pair_files([o1, o2], [c])
    assert result.pairs == []
    assert result.unpaired_candidates == [c]


# ------------------------------------------------------------------- sinks


def test_plan_patch_appends_one_op_per_value_when_field_is_empty():
    ops = plan_metadata_patch({}, FIELD, {U1: "a", U2: "b"})
    assert [op["path"] for op in ops] == [f"/metadata/{FIELD}/-"] * 2
    assert [op["value"]["value"] for op in ops] == ["a", "b"]


def test_plan_patch_replaces_own_values_and_leaves_others():
    existing = {
        FIELD: [
            {"value": "someone else's note"},
            {"value": f"{marker(U1)} old.pdf\nFAIL"},
        ]
    }
    ops = plan_metadata_patch(existing, FIELD, {U1: f"{marker(U1)} old.pdf\nPASS", U2: "new"})
    assert ops[0] == {
        "op": "replace",
        "path": f"/metadata/{FIELD}/1",
        "value": {"value": f"{marker(U1)} old.pdf\nPASS", "language": None},
    }
    assert ops[1]["path"] == f"/metadata/{FIELD}/-"


def test_plan_patch_is_idempotent():
    text = f"{marker(U1)} a.pdf\nFAIL"
    assert plan_metadata_patch({FIELD: [{"value": text}]}, FIELD, {U1: text}) == []


def test_stale_markers():
    md = {
        FIELD: [{"value": f"{marker(U1)} a"}, {"value": f"{marker(U2)} b"}, {"value": "free text"}]
    }
    assert stale_markers(md, FIELD, {U1}) == [U2]


@pytest.mark.parametrize("name", ["local", "local.a.b.c", "local a.b", "dc.title,x"])
def test_split_field_rejects_bad_names(name):
    with pytest.raises(ValueError, match="not a metadata field name"):
        split_field(name)


def test_split_field():
    assert split_field("local.accessibility.report") == ("local", "accessibility", "report")
    assert split_field("local.a11y") == ("local", "a11y", None)


# ------------------------------------------------------------------- state


def record(**kw):
    base = {
        "bitstream_uuid": U1,
        "item_uuid": "i",
        "checksum": "md5:abc",
        "engine_version": "0.2.0",
        "policy_hash": "h",
        "verdict": "FAIL",
    }
    return {**base, **kw}


def test_result_store_cache(tmp_path):
    store = ResultStore(tmp_path / "r.jsonl")
    store.append(record())
    reloaded = ResultStore(tmp_path / "r.jsonl")
    assert reloaded.cached(U1, "md5:abc", "0.2.0", "h")
    assert reloaded.cached(U1, "md5:other", "0.2.0", "h") is None
    assert reloaded.cached(U1, "md5:abc", "0.3.0", "h") is None
    assert reloaded.cached(U1, "md5:abc", "0.2.0", "other") is None
    assert reloaded.cached(U1, None, "0.2.0", "h") is None


def test_result_store_latest_wins_and_unfinished_checks_retry(tmp_path):
    store = ResultStore(tmp_path / "r.jsonl")
    store.append(record(verdict="FAIL"))
    store.append(record(verdict="CHECK_FAILED"))
    assert store.latest[U1]["verdict"] == "CHECK_FAILED"
    assert store.cached(U1, "md5:abc", "0.2.0", "h") is None


def test_result_store_skips_a_partial_last_line(tmp_path):
    path = tmp_path / "r.jsonl"
    path.write_text(json.dumps(record()) + "\n" + '{"bitstream_uuid": "trunc')
    assert list(ResultStore(path).latest) == [U1]


def test_host_slug():
    assert host_slug("https://repo.example.org:8443/server") == "repo.example.org-8443"


# ------------------------------------------------------------------- scope


@pytest.mark.asyncio
async def test_pdf_bitstreams_filters_original_bundle_pdfs():
    client = AsyncMock()
    client.get_item_bundles.return_value = {
        "_embedded": {
            "bundles": [{"uuid": "b1", "name": "ORIGINAL"}, {"uuid": "b2", "name": "THUMBNAIL"}]
        }
    }
    bitstreams = [
        {"uuid": "1", "name": "a.pdf", "_embedded": {"format": {"mimetype": "application/pdf"}}},
        {
            "uuid": "2",
            "name": "b.PDF",
            "_embedded": {
                "format": {"mimetype": "application/octet-stream", "supportLevel": "UNKNOWN"}
            },
        },
        {
            "uuid": "3",
            "name": "c.docx",
            "_embedded": {"format": {"mimetype": "application/msword"}},
        },
    ]

    async def iter_pages(endpoint, *args, **kwargs):
        assert endpoint == "core/bundles/b1/bitstreams"
        for b in bitstreams:
            yield b

    client.iter_pages = iter_pages
    targets = await pdf_bitstreams(client, ScopedItem(item={"uuid": "i"}, state="archived"))
    assert [(t.uuid, t.format_known) for t in targets] == [("1", True), ("2", False)]


# ------------------------------------------------------------------ engine

requires_outloud = pytest.mark.skipif(
    importlib.util.find_spec("outloud") is None,
    reason='needs the a11y extra (pip install -e ".[a11y]")',
)


def make_pdfs(tmp_path):
    import pymupdf

    text_pdf = tmp_path / "text.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Annual report 1998", fontsize=24)
    doc.save(text_pdf)
    scan_pdf = tmp_path / "scan.pdf"
    scan = pymupdf.open()
    scan_page = scan.new_page(width=page.rect.width, height=page.rect.height)
    scan_page.insert_image(scan_page.rect, pixmap=page.get_pixmap(dpi=72))
    scan.save(scan_pdf)
    return text_pdf, scan_pdf


@requires_outloud
def test_image_only_pages(tmp_path):
    from pdf_accessibility.engine import image_only_pages

    text_pdf, scan_pdf = make_pdfs(tmp_path)
    assert image_only_pages(str(text_pdf)) == {"pages": [], "page_count": 1}
    assert image_only_pages(str(scan_pdf)) == {"pages": [1], "page_count": 1}


@requires_outloud
def test_engine_checks_files_and_survives_a_timeout(tmp_path):
    import outloud
    from pdf_accessibility.engine import CheckEngine, EngineConfig

    text_pdf, scan_pdf = make_pdfs(tmp_path)

    async def run():
        async with CheckEngine(EngineConfig(workers=1, timeout_seconds=60)) as engine:
            scan = await engine.check(scan_pdf)
            missing = await engine.check(tmp_path / "missing.pdf")
            engine.config.timeout_seconds = 0.0001
            timed_out = await engine.check(text_pdf)
            engine.config.timeout_seconds = 60
            after = await engine.check(text_pdf)  # a fresh worker took over
        return scan, missing, timed_out, after

    scan, missing, timed_out, after = asyncio.run(run())
    assert scan.status == "ok"
    assert scan.raw["ocr"]["pages"] == [1]
    assert missing.status == "ok"
    assert missing.raw["error"]
    assert timed_out.status == "timeout"
    assert after.status == "ok"
    assert after.raw["engine"]["outloud"] == outloud.__version__


# --------------------------------------------------------------------- ocr

from pdf_accessibility.ocr_track import needs_ocr, ocr_name, pdf_language


def test_needs_ocr():
    assert needs_ocr({"analysis": {"findings": [{"rule": "OCR-001"}]}})
    assert not needs_ocr({"analysis": {"findings": [{"rule": "DOC-002"}]}})
    assert not needs_ocr({"analysis": None})


@pytest.mark.parametrize(
    ("value", "expected"),
    [("en_US", "en-US"), ("nl", "nl"), ("", "en"), ("other", "en"), ("*", "en")],
)
def test_pdf_language(value, expected):
    item = {"metadata": {"dc.language.iso": [{"value": value}]}} if value else {"metadata": {}}
    assert pdf_language(item, "en") == expected


def test_ocr_name():
    assert ocr_name("scan.pdf", "alongside") == "scan_ocr.pdf"
    assert ocr_name("scan.pdf", "replace") == "scan.pdf"
    assert ocr_name("noext", "alongside") == "noext_ocr.pdf"


@requires_outloud
def test_text_layer_is_extractable_embedded_and_lang_tagged(tmp_path):
    """The invisible text layer must give back the right text and pass the font checks."""
    import pikepdf
    import pymupdf
    from outloud import check
    from pdf_accessibility.ocr import finish_pdf, write_text_layer

    _, scan_pdf = make_pdfs(tmp_path)
    out = tmp_path / "ocr.pdf"
    with pymupdf.open(scan_pdf) as doc:
        w, h = int(doc[0].rect.width), int(doc[0].rect.height)  # as if rendered at 72 dpi
        box = [[72, 80], [400, 80], [400, 104], [72, 104]]
        write_text_layer(doc, [(0, w, h, [("Annual report 1998 – Zürich", box)])])
        doc.save(out)
    finish_pdf(str(out), "en-US")

    with pymupdf.open(out) as doc:
        assert doc[0].get_text().strip() == "Annual report 1998 – Zürich"
        words = doc[0].get_text("words")
        assert 70 <= words[0][0] <= 74  # placed where the line was recognised
    with pikepdf.open(out) as pdf:
        assert str(pdf.Root.Lang) == "en-US"
        fonts = list(pdf.pages[0].Resources.Font.values())
        assert len(fonts) == 1
        descendant = fonts[0].DescendantFonts[0]
        assert descendant.CIDToGIDMap == pikepdf.Name.Identity
        assert "/FontFile2" in descendant.FontDescriptor  # embedded
    rules = {f.rule for f in check(str(out), source=str(scan_pdf)).findings}
    assert not {r for r in rules if r.startswith(("TXT-", "SEM-"))}
    assert "DOC-013" not in rules


def test_ocr_text_on_rotated_page_follows_display(tmp_path):
    import pymupdf
    from pdf_accessibility.ocr import write_text_layer

    if importlib.util.find_spec("fontTools") is None:
        pytest.skip("needs fonttools")
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.set_rotation(90)  # displayed as 792 x 612
    w, h = int(page.rect.width), int(page.rect.height)
    write_text_layer(
        doc, [(0, w, h, [("Hello world", [[100, 80], [300, 80], [300, 100], [100, 100]])])]
    )
    words = doc[0].get_text("words")
    assert [x[4] for x in words] == ["Hello", "world"]
    assert words[0][0] < words[1][0]  # reads left to right as displayed
    assert 95 <= words[0][0] <= 105
