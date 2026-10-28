# Подключение Reddit к TgFarm

Эта инструкция предназначена для владельца аккаунта и приложения. Reddit-адаптер публикует только от имени аккаунта, чьи OAuth-данные ты настроил.

## 1. Проверь право использовать API

Открой [официальную документацию Reddit Data API](https://www.reddit.com/dev/api/) и [Data API Wiki](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki). Создай приложение в [настройках приложений Reddit](https://www.reddit.com/prefs/apps/), если у тебя есть доступ к регистрации приложения и API.

Выбери тип **web app**, укажи точный redirect URI, например:

    http://localhost:8787/callback

Сохрани client ID (строка под названием приложения) и client secret. Redirect URI должен совпадать в приложении и OAuth-запросе посимвольно.

Если Reddit требует одобрения/регистрации для нужного сценария или не предоставляет доступ к Data API, сначала реши это с Reddit. Не пытайся обходить лимиты или разрешения.

## 2. Получи permanent refresh token

В браузере открой адрес авторизации, подставив свои значения. Кодирование redirect URI важно; укажи случайное непредсказуемое значение state:

    https://www.reddit.com/api/v1/authorize?client_id=YOUR_CLIENT_ID&response_type=code&state=YOUR_RANDOM_STATE&redirect_uri=http%3A%2F%2Flocalhost%3A8787%2Fcallback&duration=permanent&scope=submit

После подтверждения Reddit перенаправит браузер на callback. Убедись, что возвращённый state точно совпадает с использованным. Скопируй значение параметра code из callback URL. Не отправляй code, client secret или токены в чат.

Обменяй code на OAuth-токены с той же redirect URI. На Windows PowerShell используй curl.exe (именно exe, чтобы не вызвать псевдоним PowerShell):

    $ClientId = "YOUR_CLIENT_ID"
    $ClientSecret = "YOUR_CLIENT_SECRET"
    $Code = "CODE_FROM_CALLBACK"
    $RedirectUri = "http://localhost:8787/callback"
    $UserAgent = "python:tgfarm:1.0 (by /u/YOUR_REDDIT_USERNAME)"
    $Pair = $ClientId + ":" + $ClientSecret
    curl.exe -sS -u $Pair -A $UserAgent --data-urlencode "grant_type=authorization_code" --data-urlencode "code=$Code" --data-urlencode "redirect_uri=$RedirectUri" https://www.reddit.com/api/v1/access_token

Ответ с access_token и refresh_token содержит секретные данные. Не публикуй его целиком и не коммить в Git. В ферме сохраняй **refresh_token**, а не короткоживущий access token. Этот процесс предполагает, что Reddit выдал токен с duration=permanent; если refresh token не пришёл, перепроверь OAuth-поток, а не подставляй access token вместо него.

## 3. Заполни локальный .env

В корне репозитория заполни:

    REDDIT_CLIENT_ID=...
    REDDIT_CLIENT_SECRET=...
    REDDIT_REFRESH_TOKEN=...
    REDDIT_SUBREDDIT=learnpython
    REDDIT_USER_AGENT=python:tgfarm:1.0 (by /u/YOUR_REDDIT_USERNAME)

- REDDIT_SUBREDDIT задаётся без префикса r/.
- Замени имя пользователя и User-Agent на правдивые значения, которые однозначно описывают твоё приложение и аккаунт.
- Никогда не записывай пароль Reddit в настройки TgFarm. Не добавляй локальный .env в Git.
- Наличие этих полей означает только, что настройки найдены. Оно не подтверждает, что Reddit принял OAuth или разрешил публикацию.

## 4. Проверь один пост в dry-run, затем сделай live smoke-test

По умолчанию глобальный переключатель Reddit выключен. Оставь его выключенным, пока не настроены OAuth и выбранное сообщество. В центре площадок включи Reddit только после проверки правил subreddit и пригодности конкретного материала для публикации.

Используй post_public_id существующей записи, которая уже имеет статус published в TgFarm:

    # Сначала TGFARM_MODE=dry в .env: команда не должна ничего отправлять наружу.
    python -m tgfarm.cli reddit-test POST_PUBLIC_ID

    # После проверки настроек, правил сообщества и предпросмотра:
    # выставить TGFARM_MODE=live и явно подтвердить единственную отправку.
    python -m tgfarm.cli reddit-test POST_PUBLIC_ID --confirm-live

В live-режиме флаг --confirm-live обязателен. Команда тестирует одну публикацию в настроенный subreddit; она не выбирает случайные сообщества и не делает массовую рассылку. Уже опубликованный в Reddit материал не будет отправлен повторно, если общий журнал знает о предыдущем результате. Если результат помечен как unknown, сначала проверь Reddit вручную, не повторяй отправку вслепую.

## 5. Правила и границы

TgFarm использует Reddit OAuth Data API; пароль аккаунта не нужен. Адаптер создаёт self-post по умолчанию, а link-post доступен только когда workflow явно задаёт meta.reddit_kind="link" и корректный payload.link.

Адаптер не ставит голоса, не публикует комментарии, не рассылает личные сообщения и не публикует один текст сразу в множество subreddit. Пиши для выбранного сообщества, соблюдай его правила, отмечай собственную аффилиацию при необходимости и учитывай ограничения Reddit API. Не включай production-публикацию только по факту того, что токен появился в настройках.
