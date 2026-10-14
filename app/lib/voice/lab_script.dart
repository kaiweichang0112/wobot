/// What Voice lab asks to be read (D5, DEC-092): questions Wobot is asked, most from
/// the eval datasets, in Chinese (16, ten with names), English (4) and both (4).
const labScript = [
  ('STT-01', 'GRC 是哪一年成立的？'),
  ('STT-02', '徐業良教授的研究領域是什麼？'),
  ('STT-03', 'G-Tech 這家公司是做什麼的？'),
  ('STT-04', 'WhizPad 的布套可以拆下來洗嗎？'),
  ('STT-05', '幫我寫一句鼓勵長輩多運動的話，另外 WhizToys 運動地墊是哪家公司的產品？'),
  ('STT-06', '元智大學的 GRC 在桃園哪裡？'),
  ('STT-07', '世大智科有哪些照護產品？'),
  ('STT-08', 'OGA 雷達毫米波生理感測裝置多少錢？'),
  ('STT-09', '請分年度列出 GRC 2021、2022 年碩士畢業生與各年人數。'),
  ('STT-10', '林怡錦的論文題目是什麼？'),
  ('STT-11', '我爸爸晚上常起床上廁所，我怕他跌倒，有什麼產品可以幫忙？'),
  ('STT-12', '家裡的長輩臥床，需要離床警示，不要穿戴、不要攝影機。'),
  ('STT-13', '有沒有不需要插電的？'),
  ('STT-14', '在家裡。'),
  ('STT-15', '都可以，你推薦就好。'),
  ('STT-16', '今天好熱，有點不想出門。'),
  ('STT-17', 'When was GRC founded?'),
  (
    'STT-18',
    'Recommend one smart-care product for detecting falls at home without a '
        'wearable device.',
  ),
  ('STT-19', 'What does G-Tech make, and where is it based?'),
  ('STT-20', 'Thanks, that was really clear.'),
  ('STT-21', '我想找一個 bed exit alarm，最好可以用手機 App 看。'),
  ('STT-22', '那個 sensor 需要充電嗎？'),
  ('STT-23', '可以幫我 compare 一下 WhizPad 跟 OGA 嗎？'),
  ('STT-24', '請列出 GRC 2024 年的 journal papers。'),
];
