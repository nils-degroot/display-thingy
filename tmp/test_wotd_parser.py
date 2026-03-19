"""Quick parser test for the WOTD template parser.

Uses hardcoded wikitext snippets so results are deterministic and don't
depend on which year's word is currently on the page.
"""

from display_thingy.views.wiktionary import _parse_wotd_template


def test_simple_inline():
    """Simple inline definition (no # prefix)."""
    wikitext = (
        "{{WOTD|pandamonium|n"
        "|{{lb|en|humorous}} [[furore|Furore]] caused by "
        "[[giant panda]]s or the [[presence]] of giant pandas."
        "|August|3}}"
    )
    wotd = _parse_wotd_template(wikitext)
    assert wotd.word == "pandamonium", f"word={wotd.word!r}"
    assert wotd.part_of_speech == "noun", f"pos={wotd.part_of_speech!r}"
    assert len(wotd.definitions) >= 1, f"defs={len(wotd.definitions)}"
    assert "Furore" in wotd.definitions[0], f"def[0]={wotd.definitions[0]!r}"
    print(f"OK: pandamonium - {len(wotd.definitions)} defs")
    for i, d in enumerate(wotd.definitions):
        print(f"  {i+1}. {d}")


def test_label_only_with_subdefs():
    """Label-only # lines with ## sub-definitions (attain-like pattern)."""
    wikitext = """{{WOTD|attain|v|# {{lb|en|transitive}}
## To [[come#Verb|come]] to or [[reach#Verb|reach]] (a [[place]]) by [[motion]] or [[progression]].
## To [[achieve#Verb|achieve]] or [[accomplish#Verb|accomplish]] (a [[goal]], etc.).
# {{lb|en|intransitive}} Often followed by ''to''.
## To come to or arrive at a particular [[state#Noun|state]] or [[condition#Noun|condition]].
|April|7}}"""
    wotd = _parse_wotd_template(wikitext)
    assert wotd.word == "attain", f"word={wotd.word!r}"
    assert wotd.part_of_speech == "verb", f"pos={wotd.part_of_speech!r}"
    assert len(wotd.definitions) >= 3, f"defs={len(wotd.definitions)}, expected>=3"
    # The (transitive) label should be prepended to its sub-defs.
    assert "(transitive)" in wotd.definitions[0], f"def[0]={wotd.definitions[0]!r}"
    print(f"OK: attain - {len(wotd.definitions)} defs")
    for i, d in enumerate(wotd.definitions):
        print(f"  {i+1}. {d}")


def test_tl_named_params():
    """tl= named params (lurgy-like pattern) should not leave stray '|'."""
    wikitext = """{{WOTD|lurgy|n|tl=humorous|tl2=slang|{{lb|en|British|informal}} An [[illness]] or [[disease]], especially one that is not [[serious#Adjective|serious]].
# {{lb|en|figurative}} A [[negative#Adjective|negative]] [[quality#Noun|quality]] or state of affairs that seems to [[spread#Verb|spread]] like a disease.
|comment=A [[w:National Sickie Day|National Sickie Day]] was celebrated on the first Monday of February.|March|2}}"""
    wotd = _parse_wotd_template(wikitext)
    assert wotd.word == "lurgy", f"word={wotd.word!r}"
    assert wotd.part_of_speech == "noun", f"pos={wotd.part_of_speech!r}"
    assert len(wotd.definitions) >= 2, f"defs={len(wotd.definitions)}, expected>=2"
    # No stray "|" at start of any definition.
    for i, d in enumerate(wotd.definitions):
        assert not d.lstrip().startswith("|"), f"def {i+1} starts with '|': {d!r}"
    # tl labels should be prepended to the first def.
    assert "(humorous" in wotd.definitions[0], f"def[0]={wotd.definitions[0]!r}"
    print(f"OK: lurgy - {len(wotd.definitions)} defs")
    for i, d in enumerate(wotd.definitions):
        print(f"  {i+1}. {d}")
    if wotd.comment:
        print(f"  comment: {wotd.comment}")


def test_secondary_word_block():
    """Template with secondary word block (bode-like pattern)."""
    wikitext = """{{WOTD|bode|v|# {{lb|en|transitive}}
## To [[indicate#Verb|indicate]] by [[sign]]s; to [[foretell]], to [[presage#Verb|presage]].
## To be an [[omen#Noun|omen]] of (a particular outcome).
# {{lb|en|intransitive}} To [[foreshow#Verb|foreshow]] something; to be an omen.

'''bode''' ''n''
# An [[omen]].
|February|23}}"""
    wotd = _parse_wotd_template(wikitext)
    assert wotd.word == "bode", f"word={wotd.word!r}"
    assert wotd.part_of_speech == "verb", f"pos={wotd.part_of_speech!r}"
    # Should have 3 defs from the verb section, not the noun section.
    assert len(wotd.definitions) >= 3, f"defs={len(wotd.definitions)}, expected>=3"
    assert "(transitive)" in wotd.definitions[0], f"def[0]={wotd.definitions[0]!r}"
    print(f"OK: bode - {len(wotd.definitions)} defs")
    for i, d in enumerate(wotd.definitions):
        print(f"  {i+1}. {d}")


def test_live_today():
    """Fetch today's actual WOTD and verify it parses without error."""
    from display_thingy.views.wiktionary import fetch_word_of_the_day
    wotd = fetch_word_of_the_day()
    assert wotd.word, "word should not be empty"
    assert wotd.definitions, "definitions should not be empty"
    print(f"OK: today's word = {wotd.word!r} ({wotd.part_of_speech}), {len(wotd.definitions)} defs")
    for i, d in enumerate(wotd.definitions):
        print(f"  {i+1}. {d}")
    if wotd.comment:
        print(f"  comment: {wotd.comment}")


def test_live_misnomer():
    """Fetch the current Aug 3 page (misnomer) which has the label-only pattern."""
    import httpx
    resp = httpx.get(
        "https://en.wiktionary.org/w/api.php",
        params={
            "action": "parse",
            "page": "Wiktionary:Word of the day/2025/August_3",
            "prop": "wikitext",
            "format": "json",
        },
        headers={"User-Agent": "display-thingy/0.1 (parser test)"},
        timeout=15,
    )
    wikitext = resp.json()["parse"]["wikitext"]["*"]
    wotd = _parse_wotd_template(wikitext)
    assert wotd.word == "misnomer", f"word={wotd.word!r}"
    assert len(wotd.definitions) >= 3, f"defs={len(wotd.definitions)}, expected>=3"
    print(f"OK: misnomer (live) - {len(wotd.definitions)} defs")
    for i, d in enumerate(wotd.definitions):
        print(f"  {i+1}. {d}")


def main():
    tests = [
        test_simple_inline,
        test_label_only_with_subdefs,
        test_tl_named_params,
        test_secondary_word_block,
        test_live_misnomer,
        test_live_today,
    ]
    failed = 0
    for test in tests:
        print(f"\n{'='*60}")
        print(f"Running: {test.__name__}")
        try:
            test()
        except Exception as e:
            print(f"FAIL: {e}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
