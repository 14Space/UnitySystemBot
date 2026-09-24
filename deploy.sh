#!/usr/bin/env bash
# Раскатка бота на сервере – с проверкой, а не вслепую.
#
# Зачем. Раньше раскатка была одной командой «собрать и запустить»: что собралось, то и
# заменяло работающего бота. Тесты гонялись только на компьютере разработчика и только
# если о них вспомнили. 24.09.2026 так на сервер уехала правка, после которой проверка
# «SoundCloud трек» покраснела, – ошибку увидели уже в отчёте с работающего бота.
#
# Как. Работающий бот не трогается, пока новая версия не докажет, что она целая:
#   1. забираем код;
#   2. запоминаем образ работающего бота (на случай отката);
#   3. собираем НОВЫЙ образ – работающий контейнер при этом продолжает работать;
#   4. гоняем все тесты ВНУТРИ нового образа: те же библиотеки и версии, что будут в
#      работе. Упал хоть один тест – останавливаемся, бот остаётся прежним;
#   5. заменяем бота и ждём, пока он станет здоровым (docker healthcheck). Не стал за
#      три минуты – возвращаем прежний образ.
# Проверка площадок после старта идёт как и раньше – её запускает сам бот, отчёт
# приходит админу.
#
# Запуск (на сервере, из папки проекта):
#     ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

# Сервер без видеокарты – поэтому второй файл обязателен (см. docker-compose.nogpu.yml).
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.nogpu.yml)
IMAGE=unitysystem-bot
CONTAINER=unitysystem-bot-1
HEALTH_WAIT=180

step() { echo; echo "== $*"; }

rollback_tag() {
    # Возвращаем метке latest прежний образ: иначе следующий «docker compose up» поднял
    # бы непроверенную сборку.
    if docker image inspect "$IMAGE:rollback" >/dev/null 2>&1; then
        docker tag "$IMAGE:rollback" "$IMAGE:latest"
    fi
}

step "1/5 Забираю код"
git pull --ff-only
git log --oneline -1

step "2/5 Запоминаю работающий образ для отката"
if docker image inspect "$IMAGE:latest" >/dev/null 2>&1; then
    docker tag "$IMAGE:latest" "$IMAGE:rollback"
    echo "прежний образ сохранён как $IMAGE:rollback"
else
    echo "работающего образа нет – откатывать будет некуда (первая установка)"
fi

step "3/5 Собираю новый образ (бот пока работает на прежнем)"
"${COMPOSE[@]}" build bot

step "4/5 Тесты внутри нового образа"
# Копию проекта кладём внутрь временного контейнера: тесты пишут временные файлы, а
# настоящие данные бота (база, куки, загрузки) им не нужны и не должны быть видны –
# поэтому data/ не копируем, кроме образца голоса, на котором проверяется расшифровка.
# .env внутрь тоже не попадает: тесты подставляют безобидные значения сами.
if ! docker run --rm -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD":/src:ro "$IMAGE:latest" sh -c '
        set -e
        mkdir -p /tmp/src/data
        tar -C /src --exclude=./.git --exclude=./data -cf - . | tar -C /tmp/src -xf -
        cp -r /src/data/samples /tmp/src/data/
        cd /tmp/src
        pip install -q --no-cache-dir --disable-pip-version-check -r requirements-dev.txt
        python -m pytest -q -p no:cacheprovider'; then
    rollback_tag
    echo
    echo "!! Тесты не прошли – бот НЕ тронут, работает прежняя версия."
    exit 1
fi

step "5/5 Заменяю бота и жду, пока он станет здоровым"
"${COMPOSE[@]}" up -d bot
deadline=$((SECONDS + HEALTH_WAIT))
status=""
while [ $SECONDS -lt $deadline ]; do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo "нет")
    [ "$status" = "healthy" ] && break
    sleep 5
done
if [ "$status" != "healthy" ]; then
    echo "!! Новый бот за ${HEALTH_WAIT}с не стал здоровым (статус: $status) – откатываю."
    docker logs --tail 40 "$CONTAINER" 2>&1 || true
    rollback_tag
    "${COMPOSE[@]}" up -d bot
    exit 1
fi

echo
echo "Готово: бот обновлён и здоров. Проверку площадок он запустит сам, отчёт придёт админу."
