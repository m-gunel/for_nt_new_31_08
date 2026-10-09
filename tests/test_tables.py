"""Таблицы и пачки строк (src/parsers/_tables.py) с настоящим токенизатором."""

from src.parsers._tables import Budget, split_parts


def test_prefix_over_budget_does_not_split_part_per_word(config):
    count = config.ov_model.count_tokens
    budget = Budget(450, count)
    prefix = (
        "Сведения о кредитах, предоставленных юридическим лицам и индивидуальным "
        "предпринимателям, в разрезе видов экономической деятельности. "
    ) * 30 + "\n"
    part = " ".join(f"Показатель{i}" for i in range(150))
    pieces = split_parts(prefix, [part], budget)
    assert count(prefix) > 450
    assert len(pieces) <= 8  # без правки — по куску на каждое слово
    assert all(count(p) <= 450 for p in pieces)
    joined = " ".join(pieces)
    assert all(f"Показатель{i}" in joined for i in range(150))
