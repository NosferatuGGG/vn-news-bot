"""Дешёвый предфильтр по ключевым словам — чтобы не гонять через нейросеть
новости про пленумы, удобрения и дороги. Всё, что прошло, оценивает модель."""
import re

# Сильные сигналы: почти всегда интересно нашей аудитории
STRONG = [
    r"\bNga\b", r"người Nga", r"khách Nga", r"quốc tịch Nga", r"Russia", r"Russian",
    r"người nước ngoài", r"du khách", r"khách (Tây|quốc tế|nước ngoài)", r"foreigner", r"tourist",
    r"Nha Trang", r"Khánh Hòa", r"Cam Ranh", r"Ninh Hòa", r"Đà Lạt", r"Phú Quốc", r"Đà Nẵng",
    r"Vũng Tàu", r"Mũi Né", r"Phan Thiết", r"Hội An", r"Quy Nhơn",
    r"thị thực", r"\bvisa\b", r"miễn thị thực", r"xuất nhập cảnh", r"tạm trú", r"trục xuất", r"deport",
    r"Aeroflot", r"bay thẳng", r"Interpol", r"truy nã quốc tế",
]

# Криминал и происшествия — интересно, если громко
CRIME = [
    r"giết", r"sát hại", r"án mạng", r"thi thể", r"tử vong", r"chết",
    r"cướp", r"trộm", r"giật", r"lừa đảo", r"lừa", r"chiếm đoạt", r"tiền giả",
    r"ma túy", r"cần sa", r"bóng cười", r"đánh nhau", r"hành hung", r"ẩu đả", r"dao", r"súng",
    r"đuối nước", r"chết đuối", r"sóng", r"sứa", r"cá mập", r"tai nạn", r"cháy", r"nổ",
    r"bắt cóc", r"truy nã", r"bắt giữ", r"khởi tố", r"mại dâm", r"đánh bạc", r"casino",
    r"murder", r"robbery", r"scam", r"arrest", r"drowning",
]

# Законы и быт, которые касаются каждого
LAWS = [
    r"quy định mới", r"từ ngày \d", r"có hiệu lực", r"mức phạt", r"phạt tiền", r"cấm",
    r"bằng lái", r"giấy phép lái xe", r"xe máy", r"thuốc lá điện tử", r"vape", r"shisha",
    r"tăng giá", r"giá điện", r"giá xăng", r"tỷ giá", r"mất điện", r"cúp nước",
    r"bão", r"áp thấp", r"lũ", r"ngập", r"mưa lớn", r"sạt lở",
    r"sân bay", r"chuyến bay", r"hãng bay", r"Long Thành",
    r"hút thuốc", r"smoking", r"\bbans? (on|at)\b", r"\bbans? \w+ing\b",
    r"\bfined?\b", r"new rules?", r"takes? effect",
]

# «Nga» ищем с учётом регистра (иначе ловит имена и случайные слоги), остальное — без
_nga = re.compile(r"\bNga\b")
_strong = re.compile("|".join(p for p in STRONG if p != r"\bNga\b"), re.IGNORECASE)
_crime = re.compile("|".join(CRIME), re.IGNORECASE)
_laws = re.compile("|".join(LAWS), re.IGNORECASE)

# Явный скучняк — отсекаем сразу
BORING = re.compile(
    r"Hội nghị|Ban Chấp hành|Đảng ủy|Tỉnh ủy|Nghị quyết|tiếp công dân|bồi dưỡng|tập huấn|"
    r"chuyển đổi số|nông thôn mới|phong trào|thi đua|khen thưởng|kỷ niệm|đại hội|"
    r"giải ngân|đầu tư công|quy hoạch",
    re.IGNORECASE,
)


def prefilter(title: str, summary: str) -> tuple[bool, str]:
    text = f"{title} {summary}"
    strong = bool(_nga.search(text) or _strong.search(text))
    if BORING.search(title) and not strong:
        return False, "boring"
    if strong:
        return True, "strong"
    if _crime.search(text):
        return True, "crime"
    if _laws.search(text):
        return True, "laws"
    return False, "no-keywords"
