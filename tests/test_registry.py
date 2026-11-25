import pytest

from kalan.parsers import MODULE_NAME_RE, UnknownFormatError, all_parsers, detect_parser


def test_module_name_rule():
    assert MODULE_NAME_RE.match("tr_vakifbank_worldcard")
    assert MODULE_NAME_RE.match("br_nubank_mastercard")
    assert not MODULE_NAME_RE.match("base")
    assert not MODULE_NAME_RE.match("vakifbank")


def test_every_parser_is_well_formed():
    for p in all_parsers():
        assert MODULE_NAME_RE.match(p.id)
        assert p.bank and p.product and p.currency and len(p.country) == 2
        assert p.amount_format is not None


def test_unknown_text_raises():
    with pytest.raises(UnknownFormatError):
        detect_parser("hello, this is not a statement")
