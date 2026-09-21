"""norm() / clean_text() —— 对照 docs/06 §2.9.2 / §2.9.5 与 R6-46 终审的全库统计口径。"""
from qtrade_agent.qidian_faces import FACE_NAMES
from qtrade_agent.text import FACE_PLACEHOLDER, clean_text, has_control_chars, norm


def test_norm_nfkc_collapse_strip():
    assert norm("收到　ＯＫ") == "收到 OK"                 # 全角空格/全角字母 NFKC 抹平
    assert norm("  1Y\n1.70\t2Y   1.80 \r\n") == "1Y 1.70 2Y 1.80"
    assert norm(None) == "" and norm("") == "" and norm("   ") == ""
    s = "a\u0014Bb"
    assert norm(s) == s                                  # 不剥 U+0014
    assert norm("[表情][图片]") == "[表情][图片]"          # 不改占位字面量
    assert norm("[玫瑰][动图 64×64]") == "[玫瑰][动图 64×64]"
    assert norm(norm("Ｑ  T")) == norm("Ｑ  T")           # 幂等 ⇒ fingerprint 只算一次也成立


def test_clean_text_face_escape_pairs():
    # R6-66:索引 → 企点 APK 里的表情名(A=65 爱你,B=66 咖啡,\t=9 流泪,¸=184 红包,\x08=8 玫瑰)
    assert clean_text("收到\u0014A") == "收到[爱你]"
    assert clean_text("\u0014A\u0014B") == "[爱你][咖啡]"                # 真实的连续两个表情各出一个
    assert clean_text("\u0014\u0014A") == "[害羞]A"                     # 不重叠:第二个 U+0014(=20)被当索引消耗
    assert clean_text("收到\u0014") == "收到"                            # 串尾孤零只丢自己
    assert clean_text("a\u0014\tb") == "a[流泪]b"                       # 后继是 \t 也一并消耗(它是索引 9)
    assert clean_text("\u0014¸") == "[红包]"                            # 索引 ≥ U+0080(字符层面,不破坏 UTF-8)
    assert clean_text("谢谢\u0014\x08") == "谢谢[玫瑰]"


def test_clean_text_unknown_face_index_falls_back_to_placeholder():
    assert clean_text("\u0014\u00dd") == FACE_PLACEHOLDER               # 221 越界(表只有 0~219)
    assert clean_text("\u0014\u4e00") == FACE_PLACEHOLDER
    assert FACE_PLACEHOLDER == "[表情]"


def test_face_table_matches_apk_extract():
    assert len(FACE_NAMES) == 220 and all(FACE_NAMES)
    assert (FACE_NAMES[0], FACE_NAMES[8], FACE_NAMES[23], FACE_NAMES[56], FACE_NAMES[64], FACE_NAMES[219]) == \
        ("呲牙", "玫瑰", "微笑", "抱拳", "OK", "口罩护体")
    assert not any("[" in n or "]" in n or n.startswith("/") for n in FACE_NAMES)   # 占位 [名称] 不会被名字本身打断


def test_clean_text_only_face_not_empty():
    assert clean_text("\u0014A") == "[爱你]"                           # 17 条纯表情消息:不为空、不丢
    assert clean_text("\u0014\u00dd") == "[表情]"


def test_clean_text_drops_other_controls_keeps_tnr():
    assert clean_text("a\x00b\x03c\x08d\te\nf\rg") == "abcd\te\nf\rg"


def test_has_control_chars_is_r6_48_predicate():
    assert has_control_chars("收到\u0014A")
    assert has_control_chars("收到\x08")
    assert not has_control_chars("1Y\n1.70\n2Y 1.80")
    assert not has_control_chars("收到👌")
    assert not has_control_chars("收到　ＯＫ")
