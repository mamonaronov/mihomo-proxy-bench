# mihomo-proxy-bench

Сравнение yaml-схем Mihomo на той же VPS, где уже крутится прод. Поднимает N тестовых контейнеров (по одному на `configs/*.yaml`) и Telegram-бота со сводкой в духе «🖴 Аптайм» из daily-stats. Прод-боты продолжают ходить в алиас `proxy`. Этот контур его не занимает.

Это **не** форк [mihomo-proxy](https://github.com/mamonaronov/mihomo-proxy) и не папка внутри него. Образ собирается с GitHub при каждом `./up.sh`, локальный Docker-образ `mihomo-proxy` не используется.

## Зависимость от прода

Нужен живой прод:

- контейнер `mihomo-proxy`
- Docker-сеть `telegram-proxy` (fixed name)
- DNS-алиас `proxy`

Бот ходит в Telegram Bot API **только** через прод: `socks5h://proxy:11808`. Пробы кандидатов — через `socks5h://proxy-<id>:11808` в отдельной сети `mihomo-bench`. Если прод лежит, бот не достучится до `api.telegram.org`. Сеть `telegram-proxy` здесь только `external: true` — не создаём и не удаляем её.

Не делай `docker compose down -v` у прода: сотрёшь кэш подписок.

## Запуск

```bash
cp .env.example .env
# Скопируй MIHOMO_API_SECRET и SUB1_URL…SUB5_URL из прода
# TELEGRAM_BOT_TOKEN — отдельный бот для bench, не прод
# ALLOWED_CHAT_ID — только этот чат
```

Yaml-кандидаты: имя файла без `.yaml` = id.

- `configs/prod.yaml` — текущая прод-схема → `mihomo-bench-prod`, `proxy-prod`
- `configs/urltest.yaml` — все подписки (sub1…sub5) в одном `url-test` AUTO → `proxy-urltest`
- `configs/botpath.yaml` — `url-test` только sub1+sub2+sub3, whitelist вне MATCH,AUTO → `proxy-botpath`
- `configs/fallback.yaml` — `fallback` sub3→sub1→sub2→sub4→sub5 → `proxy-fallback`
- `configs/loadbalance.yaml` — `load-balance` round-robin по sub1…sub5 → `proxy-loadbalance`
- `configs/noregion.yaml` — как прод, но FAST без регионального фильтра → `proxy-noregion`
- `configs/layered.yaml` — FAST url-test(sub3) → BACKUP url-test(sub1+sub2) → LAST fallback(sub4+sub5) → `proxy-layered`
- `configs/sub1.yaml` … `configs/sub5.yaml` — изолированная подписка: `AUTO = url-test` только этой sub → `proxy-sub1` … `proxy-sub5`. Вердикт по этим пяти id отвечает, какая подписка лучше несёт Bot API.

Можно добавить сколько угодно `configs/*.yaml`. Алиас никогда не `proxy`. Порты на хост не публикуются. В шаблоне обязательны плейсхолдеры `${SUB1_URL}` … `${SUB8_URL}` и `${MIHOMO_API_SECRET}` (иначе entrypoint не стартует), `mixed-port: 11808`, `external-controller: 0.0.0.0:19090`, группа `AUTO`.

```bash
chmod +x up.sh down.sh
./up.sh    # stop bench, wipe data/<id> (providers + cache.db), build from git, recreate
./down.sh  # docker compose down без -v
```

После `git push` в mihomo-proxy снова `./up.sh` — следующий up клонирует `main` (или `MIHOMO_PROXY_REF`) и тянет свежий `metacubex/mihomo:latest`. Незакоммиченные локальные правки прокси не подхватываются. Образ называется `mihomo-proxy-bench`, тег `mihomo-proxy` не трогаем.

Победившую схему перенеси в прод руками: скопируй yaml в `config.yaml` репозитория mihomo-proxy и задеплой прод как обычно.

## Бот

Long polling, портов на хост нет. Чужие чаты игнорируются. Рассылки по расписанию нет.

Фон каждую секунду запускает новую пробу по каждому кандидату, не дожидаясь прошлого curl: медленный контейнер не сдвигает тик остальных. Результат пишется в SQLite, когда дойдёт (`results/probes.db`). Успех = HTTP 404 на `https://api.telegram.org/bot` через SOCKS кандидата плюс `GET /proxies/AUTO`.

`/start`, `/status` и `/summary` открывают одну панель, как «🖴 Аптайм» в [daily-stats](https://github.com/mamonaronov/daily-stats): то же сообщение, кнопки периодов (5 мин … всё время), виды «Сводка» / «Ноды» / конкретный yaml. Сводка сама считает, какой кандидат лучше и по каким осям: надёжность, таймауты, скорость p50, хвост p95/p99, разброс, смена ноды, длинный простой — плюс рекомендация для ботов. «↻ Обновить» пересчитывает окно. Чтобы сравнить подписки, жми `sub1`…`sub5`; общая сводка смешивает схемы и изолированные sub.

## Холодный кэш и deadlock

Каждый `./up.sh` — отдельный прогон сравнения с **холодным** кэшем подписок: сначала останавливает тестовые контейнеры (прод не трогает), затем стирает весь `data/<id>` (yaml провайдеров и `cache.db`) и поднимает инстансы заново. Кэш прода не копируется. Строки из `results/probes.db` перед этим переносятся в `results/archive.db` с `run_id` прогона; живая база после этого пустая. Архив `./up.sh` не удаляет.

Провайдеры качаются через группу `SUBSCRIBE`: сначала `DIRECT`, потом `AUTO`. Если GitHub закрыт и кэш пустой, подписки не скачаются, пока туннель не поднимется — deadlock. Не указывай провайдерам `proxy: AUTO` в одиночку.

## Приватный mihomo-proxy

Docker собирает по git URL (`MIHOMO_PROXY_GIT_URL` + `#` + `MIHOMO_PROXY_REF`), репозиторий на диск как постоянная зависимость не клонируется.

- публичный HTTPS — значения из `.env.example` достаточны
- приватный HTTPS — credential helper или `~/.netrc` (`machine github.com login x password <token>`), либо URL с токеном только в локальном `.env`
- приватный SSH — `MIHOMO_PROXY_GIT_URL=git@github.com:mamonaronov/mihomo-proxy.git` и deploy key на хосте (`~/.ssh`)
