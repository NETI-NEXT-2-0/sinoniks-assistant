import argparse
import hashlib
import re
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from partner_agent.documents.canonical import extract_utf8
from partner_agent.documents.edited_copy import ReviewedCopy, render_copy
from partner_agent.documents.limits import MAX_EVIDENCE_CHARS
from partner_agent.retrieval.lexical import matched_terms, query_terms
from sinoniks_reference_pages import extract_pages

WINDOW = 1200
STEP = 1000
ANCHORS = {
    "K03": ("A", r"протоколам tcp, udp \(режим «transport»\)"),
    "K04": ("A", r"комплекс доставляет обратные"),
    "K05": ("A", r"комплекс запрещает доставку обратных"),
    "K06": ("A", r"если ядро не может совместить настройки"),
    "K07": ("A", r"согласование правил сетевого обмена происходит по их номеру слота"),
    "K08": ("A", r"от 1 до 256 \(количество определяется лицензией\)"),
    "K09": ("A", r"режим передачи файлов предназначен для передачи файлов"),
    "K10": ("A", r"следующих сущностей: «профиль»"),
    "K11": ("A", r"один профиль проверок может быть использован"),
    "K12": ("A", r"один пользователь может иметь доступ к одному или нескольким правилам"),
    "K13": ("A", r"приоритет отводится черному списку"),
    "K14": ("A", r"наличие функциональности в лицензии"),
    "K15": ("U", r"администратор должен ввести в сио публичный ключ"),
}
CORPUS_FILES = {
    "A": "administrator.txt",
    "U": "user.txt",
    "I": "installation.txt",
    "P": "product.txt",
    "V": "release.txt",
    "C": "certificate.txt",
    "R": "registration.txt",
}
CORPUS_ANCHORS = {
    "K01": (("P", r"устройство состоит из тр[её]х"),),
    "K02": (("P", r"исключается прямое соединение"),),
    **{question: (anchor,) for question, anchor in ANCHORS.items()},
    "K16": (("I", r"astra linux 1\.7se\b"), ("P", r"astra linux 1\.8\b")),
    "K17": (("V", r"выпустила версию 1\.9\b"),),
    "K18": (("C", r"номер сертификата 4975\. уровень доверия 4\b"),),
    "K19": (("R", r"31 января 2025 года"),),
    "K20": (("P", r"№\s*22866\b"),),
}
CONTROLS = ("K21", "K22")
ALTERNATE_QUESTIONS = {"K04", "K07", "K09", "K13", "K14", "K15"}
Block = tuple[str, int, int, str]


def reviewed_blocks(document: str, path: Path, *, numbered: bool = True) -> list[Block]:
    raw = path.read_bytes()
    if numbered:
        extract_pages(path, raw)
    document_id = uuid5(NAMESPACE_URL, document)
    copy = ReviewedCopy(
        document_id=document_id,
        version_id=uuid5(document_id, "local-review"),
        canonical_sha256=hashlib.sha256(raw).hexdigest(),
        text=raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n"),
    )
    digest = hashlib.sha256(copy.model_dump_json().encode("utf-8")).hexdigest()
    return [
        (document, 0, locator["start"], text) for _, text, _, locator in render_copy(copy, digest)
    ]


def blocks_for(document: str, path: Path, *, text_lines: bool) -> list[Block]:
    blocks = []
    document_id = uuid5(NAMESPACE_URL, document)
    for page, text in extract_pages(path, path.read_bytes()).items():
        if not text:
            continue
        if text_lines:
            document_page = extract_utf8(
                text.encode("utf-8"), document_id, uuid5(document_id, str(page)), "text/plain"
            )
            blocks.extend(
                (document, page, unit.locator.line, " ".join(unit.text.casefold().split()))
                for unit in document_page.blocks
                if len(unit.text) <= MAX_EVIDENCE_CHARS
            )
            continue
        clean = " ".join(text.casefold().split())
        for start in range(0, len(clean), STEP):
            blocks.append((document, page, start, clean[start : start + WINDOW]))
            if start + WINDOW >= len(clean):
                break
    return blocks


def check_corpus(path: Path, questions: dict[str, str], *, alternate: bool) -> bool:
    required = ALTERNATE_QUESTIONS if alternate else CORPUS_ANCHORS.keys() | set(CONTROLS)
    if missing := required - questions.keys():
        raise ValueError(f"Missing reference questions: {sorted(missing)}")
    blocks = [
        (document, offset, content, " ".join(content.casefold().split()))
        for document, name in CORPUS_FILES.items()
        for _, _, offset, content in reviewed_blocks(
            document, path / name, numbered=document in {"A", "U", "I"}
        )
    ]
    anchors = 0
    guaranteed = 0
    for question, query in questions.items():
        terms, identifiers = query_terms(query.replace("`", ""))
        scores = [len(matched_terms(terms, identifiers, content)) for _, _, content, _ in blocks]
        if question in CONTROLS:
            print(f"{question}: lexical_candidates={sum(score > 0 for score in scores)}")
        for document, pattern in CORPUS_ANCHORS.get(question, ()):
            anchors += 1
            matching = [
                index
                for index, (source, _, _, normalized) in enumerate(blocks)
                if source == document and re.search(pattern, normalized)
            ]
            best = max((scores[index] for index in matching), default=0)
            if not best:
                print(f"{question}: {document} rank=-")
                continue
            first = 1 + sum(score > best for score in scores)
            tied_without_anchor = scores.count(best) - sum(
                scores[index] == best for index in matching
            )
            last = first + tied_without_anchor
            guaranteed += last <= 8
            offsets = ",".join(str(blocks[index][1]) for index in matching if scores[index] == best)
            print(f"{question}: {document} offsets={offsets} rank={first}..{last}")
    print(f"anchors_guaranteed_in_top_8={guaranteed}/{anchors}")
    return anchors > 0 and anchors == guaranteed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("administrator_text", type=Path, nargs="?")
    parser.add_argument("user_text", type=Path, nargs="?")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--text-lines", action="store_true")
    modes.add_argument("--reviewed-copy", action="store_true")
    modes.add_argument("--corpus", type=Path)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--alternate-questions", action="store_true")
    args = parser.parse_args()
    suffix = "-alt" if args.alternate_questions else ""
    questions = dict(
        re.findall(
            rf"(?m)^\| (K\d\d){suffix} \| ([^|]+) \|",
            args.questions.read_text(encoding="utf-8-sig"),
        )
    )
    if args.corpus is not None:
        if args.administrator_text is not None or args.user_text is not None:
            parser.error("--corpus takes a directory instead of individual guide paths")
        if not check_corpus(args.corpus, questions, alternate=args.alternate_questions):
            parser.exit(1)
        return
    if args.administrator_text is None or args.user_text is None:
        parser.error("provide both guide paths or --corpus")
    if args.reviewed_copy:
        blocks = reviewed_blocks("A", args.administrator_text)
        blocks += reviewed_blocks("U", args.user_text)
    else:
        blocks = blocks_for("A", args.administrator_text, text_lines=args.text_lines)
        blocks += blocks_for("U", args.user_text, text_lines=args.text_lines)
    selected = {
        identifier: anchor for identifier, anchor in ANCHORS.items() if identifier in questions
    }
    if not args.alternate_questions and len(selected) != len(ANCHORS):
        raise ValueError(f"Missing reference questions: {sorted(ANCHORS.keys() - selected.keys())}")
    if not selected:
        raise ValueError("No questions with reference anchors")
    within_eight = 0
    for identifier, (document, pattern) in selected.items():
        expected = {
            block[:3]
            for block in blocks
            if block[0] == document and re.search(pattern, " ".join(block[3].casefold().split()))
        }
        if not expected:
            if args.text_lines or args.reviewed_copy:
                print(f"{identifier}: {document} no complete anchor in a single unit")
                continue
            raise ValueError(f"{identifier}: no matching anchor in {document}")
        query = re.sub(r"`([^`]+)`", r"\1", questions[identifier])
        terms, identifiers = query_terms(query)
        hits = sorted(
            (-len(matched_terms(terms, identifiers, block[3])), block[:3]) for block in blocks
        )
        position = next(
            (
                index
                for index, (score, location) in enumerate(hits, 1)
                if score < 0 and location in expected
            ),
            None,
        )
        if position is not None and position <= 8:
            within_eight += 1
        locator_index = 2 if args.reviewed_copy else 1
        locator_kind = "offsets" if args.reviewed_copy else "pages"
        locators = ",".join(
            str(value) for value in sorted({location[locator_index] for location in expected})
        )
        print(f"{identifier}: {document} {locator_kind}={locators} rank={position or '-'}")
    print(f"anchors_in_top_8={within_eight}/{len(selected)}")


if __name__ == "__main__":
    main()
