# mihomo-proxy-bench

Сравнение yaml-схем Mihomo на той же VPS, где уже крутится прод. Поднимает N тестовых контейнеров (по одному на `configs/*.yaml`) и Telegram-бота со `/status` и `/summary`. Прод-боты продолжают ходить в алиас `proxy`. Этот контур его не занимает.

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

- `configs/eu.yaml` — кандидат A → контейнер `mihomo-bench-eu`, алиас `proxy-eu`
- `configs/asia.yaml` — кандидат B → `mihomo-bench-asia`, `proxy-asia`

Можно добавить сколько угодно `configs/*.yaml`. Алиас никогда не `proxy`. Порты на хост не публикуются. В шаблоне обязательны плейсхолдеры `${SUB1_URL}` … `${SUB8_URL}` и `${MIHOMO_API_SECRET}` (иначе entrypoint не стартует), `mixed-port: 11808`, `external-controller: 0.0.0.0:19090`, группа `AUTO`.

```bash
chmod +x up.sh down.sh
./up.sh    # чистит data/<id>/providers, собирает образ с git --pull --no-cache, recreate
./down.sh  # docker compose down без -v
```

После `git push` в mihomo-proxy снова `./up.sh` — следующий up клонирует `main` (или `MIHOMO_PROXY_REF`) и тянет свежий `metacubex/mihomo:latest`. Незакоммиченные локальные правки прокси не подхватываются. Образ называется `mihomo-proxy-bench`, тег `mihomo-proxy` не трогаем.

Победившую схему перенеси в прод руками: скопируй yaml в `config.yaml` репозитория mihomo-proxy и задеплой прод как обычно.

## Бот

Long polling, портов на хост нет. Чужие чаты игнорируются. Рассылки по расписанию нет.

Фон ~каждые 15 с: curl через SOCKS кандидата на `https://api.telegram.org/bot` (успех = HTTP 404, как url-test mihomo) и `GET /proxies/AUTO` на API кандидата. JSONL: `results/probes.jsonl`.

- `/status` — последняя проба по каждому id: ok/fail, latency, HTTP, выбранная нода
- `/summary` — окно 1 час: success rate, p50, p95, таймауты; победитель — выше success, при равенстве ниже p95

## Холодный кэш и deadlock

Каждый up стартует с **пустым** `data/<id>/providers`. Кэш прода не копируется.

Провайдеры качаются через группу `SUBSCRIBE`: сначала `DIRECT`, потом `AUTO`. Если GitHub закрыт и кэш пустой, подписки не скачаются, пока туннель не поднимется — deadlock. Не указывай провайдерам `proxy: AUTO` в одиночку.

## Приватный mihomo-proxy

Docker собирает по git URL (`MIHOMO_PROXY_GIT_URL` + `#` + `MIHOMO_PROXY_REF`), репозиторий на диск как постоянная зависимость не клонируется.

- публичный HTTPS — значения из `.env.example` достаточны
- приватный HTTPS — credential helper или `~/.netrc` (`machine github.com login x password <token>`), либо URL с токеном только в локальном `.env`
- приватный SSH — `MIHOMO_PROXY_GIT_URL=git@github.com:mamonaronov/mihomo-proxy.git` и deploy key на хосте (`~/.ssh`)
