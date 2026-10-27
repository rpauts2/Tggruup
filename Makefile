.PHONY: help install dev desktop test lint fmt cycle demo doctor status report serve build clean backup restore

PY ?= python
CLI := $(PY) -m tgfarm.cli

help:  ## показать доступные команды
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	 | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## установить пакет
	$(PY) -m pip install -e .

dev:  ## установить с зависимостями для разработки
	$(PY) -m pip install -e ".[dev]"

desktop:  ## установить с GUI-зависимостями
	$(PY) -m pip install -e ".[desktop]"

test:  ## прогнать тесты
	$(PY) -m pytest tests -q

lint:  ## статический анализ
	$(PY) -m ruff check tgfarm tests

fmt:  ## автоисправление линтера
	$(PY) -m ruff check tgfarm tests --fix

init:  ## создать схему и загрузить конфиг
	$(CLI) init

collect:  ## собрать все источники один раз
	$(CLI) collect --all

cycle:  ## один полный цикл конвейера
	$(CLI) cycle

demo:  ## офлайн-прогон на синтетических данных
	$(CLI) demo

doctor:  ## диагностика окружения
	$(CLI) doctor

status:  ## состояние фермы
	$(CLI) status

trends:  ## горячие кластеры
	$(CLI) trends

channels:  ## инвентарь каналов
	$(CLI) channels

report:  ## отчёт по эффективности
	$(CLI) report --days 7

serve:  ## запустить планировщик и API в браузере
	$(CLI) serve

app:  ## открыть desktop-приложение
	$(CLI) desktop

build:  ## собрать dist/TgFarm/TgFarm.exe
	$(PY) -m pip install -e ".[desktop,build]"
	$(PY) -m PyInstaller packaging/tgfarm.spec --noconfirm --distpath dist --workpath build
	@echo "built: dist/TgFarm/TgFarm.exe"

backup:  ## бэкап базы и медиа
	@mkdir -p backup
	@sqlite3 tgfarm.db ".backup 'backup/tgfarm-$$(date +%F).db'"
	@tar czf backup/media-$$(date +%F).tgz data/media
	@echo "backup written to backup/"

restore:  ## восстановить последний бэкап
	@ls -t backup/tgfarm-*.db | head -1 | xargs -I{} cp {} tgfarm.db
	@echo "restored; verify with: make status"

clean:  ## удалить кэши и артефакты
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info