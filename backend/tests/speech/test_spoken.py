from wobot.speech.spoken import speakable, spoken_text


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
        "完整清單在文字裡。",
    ]


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
