import json

from wiki_seller import accounts

from .fake_telegram import CHAT_ID, TOKEN, FakeTelegram
from .test_end_to_end import make_config
from .test_telegram import bot, wait_for


def base_config(tmp_path, **extra):
    return make_config(tmp_path, "http://127.0.0.1:1", ["Tour Eiffel"], telegram_chat_id=CHAT_ID, **extra)


def test_old_single_account_files_move_to_the_admin(tmp_path):
    base = base_config(tmp_path)
    base.state_dir.mkdir()
    for name in ("session.json", "buy_cards.json", "price_cache.json", "telegram.json"):
        (base.state_dir / name).write_text("{}")
    accounts.migrate(base)
    admin = tmp_path / "state" / "users" / CHAT_ID
    assert (admin / "session.json").exists() and (admin / "buy_cards.json").exists()
    assert (base.state_dir / "price_cache.json").exists() and (base.state_dir / "telegram.json").exists()  # communs
    assert [(a.chat_id, a.name) for a in accounts.accounts(base)] == [(CHAT_ID, "admin")]
    accounts.migrate(base)  # une seule fois
    assert len(accounts.accounts(base)) == 1


def test_accounts_are_added_and_removed(tmp_path):
    base = base_config(tmp_path)
    accounts.migrate(base)
    assert accounts.add(base, "777", "Paul") and not accounts.add(base, "777", "Paulo")
    assert accounts.find(base, "777").name == "Paulo"
    paul = accounts.account_config(base, "777")
    admin = accounts.account_config(base, CHAT_ID)
    assert paul.state_dir == tmp_path / "state" / "users" / "777" and paul.telegram_chat_id == "777"
    assert paul.shared_state_dir == base.state_dir and paul.safe_cards_file != base.safe_cards_file
    assert admin.safe_cards_file == base.safe_cards_file  # anciennes listes : administrateur seulement
    assert accounts.remove(base, "777") and not accounts.remove(base, "777")
    assert (tmp_path / "state" / "users" / "777").is_dir()  # fichiers gardés


def test_bot_serves_each_conversation(tmp_path):
    from wiki_seller.lists import safe_titles
    from wiki_seller.telegram import CommandListener

    base = base_config(tmp_path, telegram_token=TOKEN)
    accounts.migrate(base)
    requested = []
    with FakeTelegram() as fake:
        base = make_config(tmp_path, "http://127.0.0.1:1", ["Tour Eiffel"], telegram_chat_id=CHAT_ID,
                           telegram_token=TOKEN, telegram_api_url=fake.url)
        listener = CommandListener(bot(tmp_path, fake), lambda chat, kind: requested.append(chat) or chat == CHAT_ID,
                                   base).start()
        try:
            assert wait_for(lambda: (tmp_path / "telegram.json").exists())
            steps = [("/safe Pomme", "555"),  # inconnu : reçoit son identifiant
                     ("/adduser 555 Paul", "1"),  # pas administrateur
                     ("/adduser 555 Paul", CHAT_ID),  # bienvenue à Paul + confirmation
                     ("/safe Pomme", "555"),
                     ("/update", "555"),  # compte qui ne tourne pas encore
                     ("/users", CHAT_ID),
                     ("/removeuser 555", CHAT_ID),
                     ("/list", "555")]
            expected = 0
            for text, chat in steps:
                fake.say(text, chat_id=chat)
                expected += 2 if text.startswith("/adduser 555 Paul") and chat == CHAT_ID else 1
                assert wait_for(lambda: len(fake.sent) == expected)
        finally:
            listener.stop()
    sent = list(zip(fake.chats, fake.sent))
    assert sent[0][0] == "555" and "Votre identifiant Telegram : 555" in sent[0][1]
    assert sent[1][0] == "1" and "identifiant Telegram : 1" in sent[1][1]
    assert {sent[2][0], sent[3][0]} == {"555", CHAT_ID}
    welcome = next(t for c, t in sent[2:4] if c == "555")
    assert welcome.startswith("✅ Bienvenue Paul") and "/buy" in welcome and "/adduser" not in welcome
    assert sent[4] == ("555", "🛡️ « Pomme » protégée : jamais défaussée, et toujours un exemplaire gardé "
                              "(1 carte(s) protégée(s)).")
    assert sent[5][0] == "555" and "pas encore actif" in sent[5][1] and requested == ["555"]
    assert sent[6][0] == CHAT_ID and "Paul · 555 · pas de session · 1 protégée(s)" in sent[6][1]
    assert "admin" in sent[6][1] and sent[7][1].startswith("Compte 555 retiré")
    assert "Votre identifiant Telegram : 555" in sent[8][1]  # retiré : de nouveau inconnu
    assert safe_titles(accounts.account_config(base, "555")) == ["Pomme"]
    assert json.loads((tmp_path / "state" / "users.json").read_text())["users"].keys() == {CHAT_ID}
