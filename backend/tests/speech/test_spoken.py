from wobot.speech.spoken import continue_from, list_page, speakable, spoken_text


def test_links_phones_and_addresses_are_not_spoken():
    line = (
        "2024/01/05　From mechanical design，地點：元智大學，PDF：https://drive.google.com/x/view"
    )

    assert "http" not in speakable(line)
    assert "地點" not in speakable(line)
    assert speakable("產品網頁：https://a.example\n廠商電話：02-1234-5678\n好用") == "好用"


def test_chat_and_knowledge_replies_are_spoken_as_they_streamed():
    assert spoken_text("knowledge", {"text": "GRC 於 2003 年成立。"}, True) is None
    assert spoken_text("chat", {"text": "你好！"}, True) is None


def test_a_list_is_spoken_as_its_intro_and_first_items():
    reply = {
        "text": "共 5 位畢業生。\n\n"
        "1. 2021　碩士　張三：《智慧床墊》，全文：https://hdl.handle.net/1\n"
        "2. 2021　碩士　李四：《地墊》\n"
        "3. 2021　碩士　王五：《手環》\n"
        "4. 2021　碩士　趙六：《機器人》",
        "lists": [{"result_id": "q-1"}],
    }

    said = spoken_text("list", reply, True)

    assert said.splitlines() == [
        "共 5 位畢業生。",
        "2021，碩士，張三：《智慧床墊》",
        "2021，碩士，李四：《地墊》",
        "2021，碩士，王五：《手環》",
        "還有 1 筆可以繼續聽。",
    ]
    assert continue_from(reply) == 3


def test_a_recommendation_says_each_product_and_its_needs_without_its_card():
    reply = {
        "text": "這是符合你需求的產品：\n\n推薦：WhizPad（世大智科）\n- 離床偵測：符合\n"
        "產品網頁：https://a.example\n廠商電話：03-1234567\n\n"
        "另有 2 項產品也符合，想看的話再跟我說。",
        "action": "recommend",
        "products": [
            {
                "id": "r-1",
                "name": "WhizPad",
                "company": "世大智科",
                "checks": {"離床偵測": "supported", "places=home": "unknown"},
            }
        ],
    }

    said = spoken_text("recommend", reply, True)

    assert said.splitlines()[:3] == [
        "這是符合你需求的產品：",
        "WhizPad，世大智科。",
        "符合離床偵測。",
    ]
    assert said.splitlines()[-1] == "另有 2 項產品也符合，想看的話再跟我說。"
    assert "03-1234567" not in said and "http" not in said


def test_a_question_is_spoken_as_written():
    reply = {"text": "嗯……你想用在家裡還是機構？", "action": "replied", "products": []}

    assert spoken_text("recommend", reply, True) == "嗯……你想用在家裡還是機構？"


def lectures(n: int) -> str:
    return f"共 {n} 場演講。\n\n" + "\n".join(
        f"{i}. 2024/01/{i:02d}　演講 {i}，地點：元智大學，PDF：https://a.example/{i}"
        for i in range(1, n + 1)
    )


def test_a_list_is_read_a_page_at_a_time_to_its_end():
    text = lectures(10)

    first, after_first = list_page(text, 3, 5, True)
    last, after_last = list_page(text, after_first, 5, True)

    assert first.splitlines() == [
        *(f"2024/01/{i:02d}，演講 {i}" for i in range(4, 9)),
        "還有 2 筆可以繼續聽。",
    ]
    assert after_first == 8
    assert last.splitlines() == ["2024/01/09，演講 9", "2024/01/10，演講 10"]
    assert after_last is None
    assert list_page(text, 3, 5, True) == (first, after_first)  # the same start, the same page
    assert list_page(text, 10, 5, True) == ("", None)


def test_a_short_list_is_said_whole_and_nothing_is_left():
    reply = {"text": lectures(3), "lists": [{"result_id": "q-1"}]}

    said = spoken_text("list", reply, True)

    assert said.splitlines()[-1] == "2024/01/03，演講 3"
    assert continue_from(reply) is None


def test_lists_are_read_with_their_headings_and_the_items_listed_apart():
    text = (
        "找到兩份清單。\n\n"
        "2021 年・碩士畢業生（2 筆）\n1. 2021　碩士　張三\n2. 2021　碩士　李四\n\n"
        "演講（1 筆）\n1. 2024/01/05　演講\n\n"
        "以下 1 筆的來源只記載年份或沒有日期，無法確定是否在 2020 至 2021 之間：\n"
        "- 2021　某演講"
    )

    first, after = list_page(text, 0, 3, True)
    rest, end = list_page(text, after, 5, True)

    assert first.splitlines() == [
        "2021 年・碩士畢業生（2 筆）",
        "2021，碩士，張三",
        "2021，碩士，李四",
        "演講（1 筆）",
        "2024/01/05，演講",
        "還有 1 筆可以繼續聽。",
    ]
    assert rest.splitlines() == [
        "以下 1 筆的來源只記載年份或沒有日期，無法確定是否在 2020 至 2021 之間：",
        "2021，某演講",
    ]
    assert end is None


def test_replies_other_than_lists_have_nothing_to_continue():
    assert continue_from({"text": "你好！"}) is None
    assert continue_from({"text": "沒有找到。", "lists": []}) is None
