"""norm() / clean_text() —— 对照 docs/06 §2.9.2 / §2.9.5 与 R6-46 终审的全库统计口径。"""
from qtrade_agent.text import FACE_PLACEHOLDER, clean_text, has_control_chars, norm


def test_norm_nfkc_collapse_strip():
    assert norm("收到　ＯＫ") == "收到 OK"                 # 全角空格/全角字母 NFKC 抹平
    assert norm("  1Y\n1.70\t2Y   1.80 \r\n") == "1Y 1.70 2Y 1.80"
    assert norm(None) == "" and norm("") == "" and norm("   ") == ""
    s = "a\u0014Bb"
    assert norm(s) == s                                  # 不剥 U+0014
    assert norm("[表情][图片]") == "[表情][图片]"          # 不改占位字面量
    assert norm(norm("Ｑ  T")) == norm("Ｑ  T")           # 幂等 ⇒ fingerprint 只算一次也成立


def test_clean_text_face_escape_pairs():
    assert clean_text("收到\u0014A") == "收到" + FACE_PLACEHOLDER
    assert clean_text("\u0014A\u0014B") == FACE_PLACEHOLDER * 2          # 真实的连续两个表情各出一个
    assert clean_text("\u0014\u0014A") == FACE_PLACEHOLDER + "A"          # 不重叠:第二个 U+0014 被当索引消耗
    assert clean_text("收到\u0014") == "收到"                            # 串尾孤零只丢自己
    assert clean_text("a\u0014\tb") == "a" + FACE_PLACEHOLDER + "b"       # 后继是 \t 也一并消耗
    assert clean_text("\u0014¸") == FACE_PLACEHOLDER                 # 索引 ≥ U+0080(字符层面,不破坏 UTF-8)


def test_clean_text_only_face_not_empty():
    assert clean_text("\u0014A") == FACE_PLACEHOLDER                      # 17 条纯表情消息:不为空、不丢


def test_clean_text_drops_other_controls_keeps_tnr():
    assert clean_text("a\x00b\x03c\x08d\te\nf\rg") == "abcd\te\nf\rg"


def test_has_control_chars_is_r6_48_predicate():
    assert has_control_chars("收到\u0014A")
    assert has_control_chars("收到\x08")
    assert not has_control_chars("1Y\n1.70\n2Y 1.80")
    assert not has_control_chars("收到👌")
    assert not has_control_chars("收到　ＯＫ")
