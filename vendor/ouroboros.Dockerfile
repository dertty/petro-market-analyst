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

# Расчётный стек скилла skills/oil_price_forecast. Стоит здесь, а не в манифесте
# скилла через dependencies, по двум причинам:
#
# 1. Изолированный .ouroboros_env не умеет ставить CPU-сборку torch. Имя пакета
#    проходит через фильтр, где символ "+" запрещён (marketplace/install_specs.py),
#    поэтому torch==X+cpu не пройдёт, а index-url задать негде: argv pip фиксирован,
#    а PIP_INDEX_URL/PIP_EXTRA_INDEX_URL вырезаются из окружения установщика.
#    Голый torch с PyPI притащил бы CUDA-сборку на несколько ГБ.
# 2. Само наличие изолированных зависимостей включает out-of-process-диспатч
#    (extension_process_runner.extension_requires_process_isolation), а в нём
#    register() выполняется заново на каждый вызов инструмента — веса модели
#    перечитывались бы с диска каждый раз. Без dependencies расширение грузится
#    внутри процесса сервера один раз, и пайплайн живёт в модульной глобали.
#
# Цена решения — примерно +700 МБ к образу и падение torch роняет server.py целиком;
# обе оговорки записаны в PROBLEMS.md.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install --no-cache-dir chronos-forecasting statsmodels pandas numpy

# Веса Chronos-Bolt кладём в образ, иначе первый вызов инструмента полез бы в сеть
# и упёрся в таймаут. HF_HOME задан явно: без него кэш уехал бы в $HOME, а контейнер
# работает от имени владельца смонтированного каталога, и домашний каталог может
# оказаться недоступен на запись.
ENV HF_HOME=/opt/huggingface
RUN python -c "from huggingface_hub import snapshot_download; snapshot_download('amazon/chronos-bolt-small')"

# Поисковый стек скилла skills/petro_rag. Тем же способом и по тем же причинам, что и
# расчётный выше: через dependencies скилла он включил бы out-of-process-диспатч.
#
# --only-binary: компилятора в образе нет, и лучше честно упасть на сборке, чем пытаться
# собрать из исходников. pypdf не указан — он уже в requirements Ouroboros.
#
# Версия starlette сверяется до и после установки: chromadb исторически тянул fastapi,
# который узко пинит starlette, а на ней держится весь веб-слой Ouroboros. Проверка, а не
# пин: пин сам стал бы даунгрейдом, когда подмодуль обновится и рантайму понадобится
# версия новее. Заодно проверяется, что слой не сломал torch предыдущего шага.
COPY vendor/requirements-rag.txt /tmp/requirements-rag.txt
RUN BEFORE="$(python -c 'import starlette; print(starlette.__version__)')" \
    && pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements-rag.txt \
    && AFTER="$(python -c 'import starlette; print(starlette.__version__)')" \
    && if [ "$BEFORE" != "$AFTER" ]; then \
           echo "FATAL: starlette съехала при установке RAG-зависимостей: $BEFORE -> $AFTER" >&2; \
           exit 1; \
       fi \
    && python -c "import chromadb, fastembed, pypdf, torch, langchain_text_splitters; print('rag deps ok')"

# Веса эмбеддера, в отличие от Chronos, лежат не в образе, а в data/models на хосте:
# multilingual-e5-large весит 2.24 ГБ, и класть его в слой значило бы перекачивать всё
# заново при каждой инвалидации кэша сборки. Каталог переживает пересборку образа.
# Скачиваются при первом запуске scripts/build_index.py.
# Настройки поиска (PETRO_RAG_*) задаются в docker-compose.yml, а не здесь: там их
# видно рядом с остальными параметрами и можно менять через .env без пересборки образа.
ENV OUROBOROS_SERVER_HOST=0.0.0.0 \
    OUROBOROS_SERVER_PORT=8765 \
    PYTHONUNBUFFERED=1

EXPOSE 8765

# Тот же вход, что у upstream. Десктопный launcher.py не используется, поэтому
# визард настройки не участвует, а ключи приходят из .env.
CMD ["python", "server.py"]
