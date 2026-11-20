from uuid import uuid4

import pytest

from partner_agent.documents.canonical import CanonicalError
from partner_agent.documents.docling_adapter import normalize_docling


def test_source_order_table_headers_and_missing_coordinates_are_preserved():
    data = {
        "body": {"children": [{"$ref": "#/tables/0"}, {"$ref": "#/texts/0"}]},
        "texts": [{"self_ref": "#/texts/0", "label": "footnote", "text": "Примечание"}],
        "tables": [
            {
                "self_ref": "#/tables/0",
                "label": "table",
                "data": {
                    "table_cells": [
                        {
                            "start_row_offset_idx": 0,
                            "start_col_offset_idx": 0,
                            "text": "Курс",
                            "column_header": True,
                        },
                        {"start_row_offset_idx": 1, "start_col_offset_idx": 0, "text": "Python"},
                    ]
                },
                "footnotes": [{"$ref": "#/texts/0"}],
            }
        ],
    }
    result = normalize_docling(data, uuid4(), uuid4(), "a" * 64, "docling-2.150.0")
    assert [b.kind for b in result.blocks] == ["table", "footnote"]
    assert result.blocks[0].text == "Курс | Python"
    assert result.blocks[0].cells[0].column_header
    assert result.blocks[0].related_paths == ("#/texts/0",)
    assert result.blocks[0].positions[0].page is None
    assert result.blocks[0].positions[0].bbox is None
    assert result.warnings == ("element_locator_without_page_bbox",)


def test_cycle_is_rejected_without_recursing_forever():
    data = {
        "body": {"children": [{"$ref": "#/groups/0"}]},
        "groups": [{"self_ref": "#/groups/0", "children": [{"$ref": "#/groups/0"}]}],
    }
    with pytest.raises(CanonicalError, match="repeated_element_reference"):
        normalize_docling(data, uuid4(), uuid4(), "a" * 64, "docling-2.150.0")


def test_exact_ooxml_footnote_relationship_survives_normalization():
    data = {
        "body": {"children": [{"$ref": "#/texts/0"}]},
        "texts": [{"self_ref": "#/texts/0", "text": "Описание курса"}],
        "partner_footnote_relationships": [
            {
                "note_id": "1",
                "paragraph_ordinal": 2,
                "text": "Материалы доступны участникам",
                "locator": "word/footnotes.xml#1",
            }
        ],
    }
    result = normalize_docling(data, uuid4(), uuid4(), "a" * 64, "docling-2.150.0")
    assert result.footnote_relationships[0].paragraph_ordinal == 2
    assert result.footnote_relationships[0].locator == "word/footnotes.xml#1"


def test_native_pdf_order_warning_remains_explicit():
    data = {
        "body": {"children": [{"$ref": "#/texts/0"}]},
        "texts": [{"self_ref": "#/texts/0", "text": "Порядок требует проверки"}],
        "partner_quality_warnings": ["native_text_order_uncertain", "untrusted_extra"],
    }
    result = normalize_docling(data, uuid4(), uuid4(), "a" * 64, "docling-2.150.0")
    assert "native_text_order_uncertain" in result.warnings
    assert "untrusted_extra" not in result.warnings


def test_uncertain_reading_order_and_unlinked_footnotes_block_readiness():
    from partner_agent.documents.docling_adapter import assert_reviewable

    data = {
        "body": {"children": [{"$ref": "#/texts/0"}]},
        "texts": [{"self_ref": "#/texts/0", "label": "footnote", "text": "Условие"}],
    }
    parsed = normalize_docling(data, uuid4(), uuid4(), "a" * 64, "docling-2.150.0")
    with pytest.raises(CanonicalError, match="unlinked_footnote_review_required"):
        assert_reviewable(parsed)
    data["partner_footnote_relationships"] = [
        {
            "note_id": "1",
            "paragraph_ordinal": 1,
            "text": "Условие",
            "locator": "word/footnotes.xml#1",
        }
    ]
    parsed = normalize_docling(data, uuid4(), uuid4(), "a" * 64, "docling-2.150.0")
    assert_reviewable(parsed)
    data["partner_quality_warnings"] = ["native_text_order_uncertain"]
    parsed = normalize_docling(data, uuid4(), uuid4(), "a" * 64, "docling-2.150.0")
    with pytest.raises(CanonicalError, match="manual_quality_review_required"):
        assert_reviewable(parsed)


@pytest.mark.parametrize(
    "bbox,origin",
    [
        ((float("nan"), 10, 20, 0), "BOTTOMLEFT"),
        ((0, 10, float("inf"), 0), "BOTTOMLEFT"),
        ((20, 10, 0, 0), "BOTTOMLEFT"),
        ((0, 0, 20, 10), "BOTTOMLEFT"),
        ((0, 10, 20, 0), "TOPLEFT"),
    ],
)
def test_invalid_citation_geometry_is_rejected(bbox, origin):
    from pydantic import ValidationError

    from partner_agent.documents.docling_adapter import SourcePosition

    with pytest.raises(ValidationError):
        SourcePosition(element_path="#/texts/0", page=1, bbox=bbox, origin=origin)


def test_missing_footnote_target_is_not_accepted_as_a_valid_link():
    data = {
        "body": {"children": [{"$ref": "#/texts/0"}]},
        "texts": [{"self_ref": "#/texts/0", "text": "Курс", "footnotes": [{"$ref": "#/texts/99"}]}],
    }
    with pytest.raises(CanonicalError, match="missing_related_reference"):
        normalize_docling(data, uuid4(), uuid4(), "a" * 64, "docling-2.150.0")


def test_deep_structure_is_bounded_before_python_recursion_limit():
    groups = [
        {"self_ref": f"#/groups/{i}", "children": [{"$ref": f"#/groups/{i + 1}"}]}
        for i in range(140)
    ]
    data = {"body": {"children": [{"$ref": "#/groups/0"}]}, "groups": groups}
    with pytest.raises(CanonicalError, match="element_depth_limit"):
        normalize_docling(data, uuid4(), uuid4(), "a" * 64, "docling-2.150.0")


def table_data(cells, rows=2, columns=2):
    return {
        "body": {"children": [{"$ref": "#/tables/0"}]},
        "tables": [
            {
                "self_ref": "#/tables/0",
                "label": "table",
                "data": {
                    "num_rows": rows,
                    "num_cols": columns,
                    "table_cells": cells,
                },
            }
        ],
    }


def test_table_order_is_geometric_and_merged_cells_are_preserved():
    data = table_data(
        [
            {"start_row_offset_idx": 1, "start_col_offset_idx": 1, "text": "24"},
            {
                "start_row_offset_idx": 0,
                "start_col_offset_idx": 0,
                "col_span": 2,
                "column_header": True,
                "text": "Программа",
            },
            {"start_row_offset_idx": 1, "start_col_offset_idx": 0, "text": "Python"},
        ]
    )
    result = normalize_docling(data, uuid4(), uuid4(), "a" * 64, "docling-2.150.0")
    assert result.blocks[0].text == "Программа | Python | 24"
    assert result.blocks[0].cells[0].column_span == 2


@pytest.mark.parametrize(
    "cells,code",
    [
        (
            [{"start_row_offset_idx": 2, "start_col_offset_idx": 0, "text": "x"}],
            "table_cell_out_of_bounds",
        ),
        (
            [
                {"start_row_offset_idx": 0, "start_col_offset_idx": 0, "col_span": 2, "text": "x"},
                {"start_row_offset_idx": 0, "start_col_offset_idx": 1, "text": "y"},
            ],
            "overlapping_table_cells",
        ),
    ],
)
def test_invalid_table_structure_is_rejected(cells, code):
    with pytest.raises(CanonicalError, match=code):
        normalize_docling(table_data(cells), uuid4(), uuid4(), "a" * 64, "docling-2.150.0")


def test_vertical_merged_condition_is_repeated_on_every_applicable_row():
    data = table_data(
        [
            {
                "start_row_offset_idx": 0,
                "start_col_offset_idx": 0,
                "column_header": True,
                "text": "Доступ",
            },
            {
                "start_row_offset_idx": 0,
                "start_col_offset_idx": 1,
                "column_header": True,
                "text": "Курс",
            },
            {
                "start_row_offset_idx": 1,
                "start_col_offset_idx": 0,
                "row_span": 2,
                "row_header": True,
                "text": "Только участникам",
            },
            {"start_row_offset_idx": 1, "start_col_offset_idx": 1, "text": "Python"},
            {"start_row_offset_idx": 2, "start_col_offset_idx": 1, "text": "SQL"},
        ],
        rows=3,
    )
    result = normalize_docling(data, uuid4(), uuid4(), "a" * 64, "docling-2.150.0")
    assert result.blocks[0].text.splitlines() == [
        "Доступ | Курс | Только участникам | Python",
        "Доступ | Курс | Только участникам | SQL",
    ]
    assert len(result.blocks[0].cells) == 5


def test_approved_disclosure_cannot_drop_or_auto_include_linked_note():
    from partner_agent.documents.docling_adapter import approved_context

    data = {
        "body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}]},
        "texts": [
            {
                "self_ref": "#/texts/0",
                "text": "Материалы доступны",
                "footnotes": [{"$ref": "#/texts/1"}],
            },
            {"self_ref": "#/texts/1", "label": "footnote", "text": "Только участникам"},
        ],
    }
    parsed = normalize_docling(data, uuid4(), uuid4(), "a" * 64, "docling-2.150.0")
    with pytest.raises(CanonicalError, match="incomplete_approved_context"):
        approved_context(parsed, frozenset([parsed.blocks[0].id]))
    with pytest.raises(CanonicalError, match="orphaned_approved_footnote"):
        approved_context(parsed, frozenset([parsed.blocks[1].id]))
    selected = approved_context(parsed, frozenset(block.id for block in parsed.blocks))
    assert [block.text for block in selected] == ["Материалы доступны", "Только участникам"]
    with pytest.raises(CanonicalError, match="unknown_approved_block"):
        approved_context(parsed, frozenset([uuid4()]))


def test_multilevel_headers_keep_row_and_column_order():
    data = table_data(
        [
            {
                "start_row_offset_idx": 0,
                "start_col_offset_idx": 0,
                "col_span": 2,
                "column_header": True,
                "text": "Учебная нагрузка",
            },
            {
                "start_row_offset_idx": 1,
                "start_col_offset_idx": 0,
                "column_header": True,
                "text": "Курс",
            },
            {
                "start_row_offset_idx": 1,
                "start_col_offset_idx": 1,
                "column_header": True,
                "text": "Часы",
            },
            {"start_row_offset_idx": 2, "start_col_offset_idx": 0, "text": "Python"},
            {"start_row_offset_idx": 2, "start_col_offset_idx": 1, "text": "24"},
        ],
        rows=3,
    )
    parsed = normalize_docling(data, uuid4(), uuid4(), "a" * 64, "docling-2.150.0")
    assert parsed.blocks[0].text == "Учебная нагрузка | Курс | Часы | Python | 24"


@pytest.mark.parametrize("corruption", ["text", "duplicate", "order", "hash"])
def test_stored_canonical_integrity_revalidated_on_json_load(corruption):
    from pydantic import ValidationError

    from partner_agent.documents.docling_adapter import ParsedDocument

    data = {
        "body": {"children": [{"$ref": "#/texts/0"}]},
        "texts": [{"self_ref": "#/texts/0", "text": "Курс Python"}],
    }
    parsed = normalize_docling(data, uuid4(), uuid4(), "a" * 64, "docling-2.150.0")
    stored = parsed.model_dump(mode="json")
    if corruption == "text":
        stored["blocks"][0]["text"] = "Изменено"
    elif corruption == "duplicate":
        stored["blocks"].append(dict(stored["blocks"][0]))
    elif corruption == "order":
        stored["blocks"][0]["ordinal"] = 8
    else:
        stored["source_sha256"] = "invalid"
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(stored)


@pytest.mark.parametrize("corruption", ["version", "block_id", "relationship", "locator"])
def test_stored_block_identity_cannot_be_transplanted(corruption):
    from pydantic import ValidationError

    from partner_agent.documents.docling_adapter import ParsedDocument

    parsed = normalize_docling(
        {
            "body": {"children": [{"$ref": "#/texts/0"}]},
            "texts": [{"self_ref": "#/texts/0", "text": "Программа курса"}],
        },
        uuid4(),
        uuid4(),
        "a" * 64,
        "docling-2.150.0",
    )
    stored = parsed.model_dump(mode="json")
    if corruption == "version":
        stored["version_id"] = str(uuid4())
    elif corruption == "block_id":
        stored["blocks"][0]["id"] = str(uuid4())
    elif corruption == "relationship":
        stored["blocks"][0]["related_paths"] = ["#/texts/99"]
    else:
        stored["blocks"][0]["positions"].append({"element_path": "#/texts/99"})
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(stored)


def test_review_context_explains_linked_and_unresolved_notes_without_approval():
    from partner_agent.documents.docling_adapter import normalize_docling, review_context

    data = {
        "body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}]},
        "texts": [
            {"self_ref": "#/texts/0", "text": "Курс", "footnotes": [{"$ref": "#/texts/1"}]},
            {"self_ref": "#/texts/1", "label": "footnote", "text": "Только участникам"},
        ],
        "partner_footnote_relationships": [
            {"note_id": "1", "paragraph_ordinal": 0, "text": "Только участникам",
             "locator": "word/footnotes.xml#1"},
        ],
    }
    canonical = normalize_docling(data, uuid4(), uuid4(), "a" * 64, "docling-test")
    related, parents, required, unresolved, quality_block = review_context(canonical)
    assert related == {canonical.blocks[0].id: (1,)}
    assert parents == {canonical.blocks[1].id: (0,)}
    assert required == (1,) and not unresolved and quality_block is None
    changed = canonical.model_copy(update={"footnote_relationships": (
        canonical.footnote_relationships[0].model_copy(update={"text": "Не найдено"}),
    )})
    assert review_context(changed)[3] is True


def test_rendered_disclosure_keeps_linked_note_and_its_provenance():
    import hashlib

    from partner_agent.documents.disclosure import render_approved_docling

    data = {
        "body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}]},
        "texts": [
            {"self_ref": "#/texts/0", "text": "Курс доступен",
             "footnotes": [{"$ref": "#/texts/1"}]},
            {"self_ref": "#/texts/1", "label": "footnote", "text": "Только участникам"},
        ],
    }
    parsed = normalize_docling(data, uuid4(), uuid4(), "a" * 64, "docling-2.150.0")
    with pytest.raises(CanonicalError, match="incomplete_approved_context"):
        render_approved_docling(parsed, frozenset([parsed.blocks[0].id]))
    units = render_approved_docling(parsed, frozenset(block.id for block in parsed.blocks))
    assert len(units) == 1
    block_id, text, digest, locator = units[0]
    assert block_id == parsed.blocks[0].id
    assert "Курс доступен" in text and "Только участникам" in text
    assert digest == hashlib.sha256(text.encode()).hexdigest()
    assert locator["context"][0]["block_id"] == str(parsed.blocks[1].id)
    data["texts"][0]["text"] = "x" * 1600
    large = normalize_docling(data, uuid4(), uuid4(), "a" * 64, "docling-2.150.0")
    from partner_agent.infra.errors import APIError

    with pytest.raises(APIError) as rejected:
        render_approved_docling(large, frozenset(block.id for block in large.blocks))
    assert rejected.value.code == "disclosure_context_too_large"


def test_unmapped_docx_notes_travel_with_each_selected_parent_without_claiming_a_link():
    from partner_agent.documents.disclosure import render_approved_docling

    data = {
        "body": {"children": [
            {"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}, {"$ref": "#/texts/2"}
        ]},
        "texts": [
            {"self_ref": "#/texts/0", "text": "Курс Python"},
            {"self_ref": "#/texts/1", "text": "Курс SQL"},
            {"self_ref": "#/texts/2", "label": "footnote", "text": "Только участникам"},
        ],
        "partner_footnote_relationships": [
            {"note_id": "1", "paragraph_ordinal": 0, "text": "Только участникам",
             "locator": "word/footnotes.xml#1"},
        ],
    }
    parsed = normalize_docling(data, uuid4(), uuid4(), "a" * 64, "docling-2.150.0")
    with pytest.raises(CanonicalError, match="orphaned_approved_footnote"):
        render_approved_docling(parsed, frozenset([parsed.blocks[2].id]))
    units = render_approved_docling(parsed, frozenset(block.id for block in parsed.blocks))
    assert len(units) == 2
    assert all("Только участникам" in unit[1] for unit in units)
    assert all("связь примечаний с фрагментом не определена" in unit[1] for unit in units)
