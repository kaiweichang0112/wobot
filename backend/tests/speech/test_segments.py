from wobot.speech.segments import Segmenter


def cut(*deltas: str) -> list[str]:
    segmenter = Segmenter()
    sentences = []
    for delta in deltas:
        sentences += segmenter.add(delta)
    return sentences + segmenter.flush()


def test_a_sentence_is_ready_as_soon_as_it_ends():
    segmenter = Segmenter()

    assert segmenter.add("找到了！廠商是醫博") == ["找到了！"]
    assert segmenter.add("科技。電話") == ["廠商是醫博科技。"]
    assert segmenter.flush() == ["電話"]


def test_the_first_sentence_ends_at_its_first_comma_so_the_voice_starts_early():
    sentences = cut("哇這題我知道，GRC 在桃園市中壢區，元智大學裡面。")

    assert sentences == ["哇這題我知道，", "GRC 在桃園市中壢區，元智大學裡面。"]


def test_an_english_full_stop_ends_a_sentence_only_before_a_space():
    assert cut("It costs 3.5 dollars. See ", "wobot.example for more.") == [
        "It costs 3.5 dollars.",
        "See wobot.example for more.",
    ]


def test_a_long_sentence_without_a_full_stop_is_cut_at_a_comma():
    text = "這是一段很長的回答，" * 8
    sentences = cut(text)

    assert all(len(s) <= 48 for s in sentences)
    assert "".join(sentences) == text


def test_marks_alone_are_not_spoken():
    assert cut("……！", "好。") == ["好。"]


def test_the_first_sentence_never_ends_at_a_list_s_mark():
    sentences = cut("哇，這張床墊把舒適、照護提醒和睡眠資料整合在一起，設計得真周到！")

    assert sentences[0] == "哇，這張床墊把舒適、照護提醒和睡眠資料整合在一起，"
