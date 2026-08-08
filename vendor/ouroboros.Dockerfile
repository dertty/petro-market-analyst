# Образ рантайма Ouroboros: только зависимости.
#
# Исходники сюда НЕ копируются — они монтируются из vendor/ouroboros вместе с корнем
# проекта. Причина в подмодуле: у него .git это файл с относительной ссылкой наружу,
# и если отдать в контейнер только vendor/ouroboros, git внутри работать не будет,
# а Ouroboros ожидает REPO_DIR настоящим git-репозиторием.
#
# Лежит рядом с подмодулем, а не внутри него: всё внутри vendor/ouroboros/ принадлежит
# истории форка, и build-конфиг там шумел бы при слияниях из managed.
#
# Отличие от upstream-образа: пропущены оба шага playwright (install-deps и install).
# Это самая тяжёлая часть сборки, а браузерные инструменты у нас в disabled_tools.
# Возвращаются двумя строками, если понадобятся.

FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Контейнер работает от имени владельца смонтированного каталога, поэтому git
# иначе ругается на «detected dubious ownership» и Ouroboros теряет репозиторий.
RUN git config --system --add safe.directory '*'

WORKDIR /workspace

COPY vendor/ouroboros/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ENV OUROBOROS_SERVER_HOST=0.0.0.0 \
    OUROBOROS_SERVER_PORT=8765 \
    PYTHONUNBUFFERED=1

EXPOSE 8765

# Тот же вход, что у upstream. Десктопный launcher.py не используется, поэтому
# визард настройки не участвует, а ключи приходят из .env.
CMD ["python", "server.py"]
