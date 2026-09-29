.PHONY: help test probe ui llm gate verify dashboard up down logs ps backup restore verify-backup migrate migrate-check revision

# The four-layer verification, in the order you should run it. Each layer
# catches a class of defect the one above it cannot:
#
#   test    in-process logic (fast, no services) — the only layer CI runs
#   probe   the real public link, real HTTP, real database
#   ui      the actual page in a real DOM (jsdom)
#   llm     the provider really was called (tokens and cost moved)
#
# A green `test` alone has never been sufficient here: several real defects in
# this project were invisible to pytest and only appeared at layer 2 or 3.
help:
	@echo "四层验证:"
	@echo "  make test            进程内逻辑（pytest）"
	@echo "  make probe URL=...   公网真实链路（acceptance_probe.py）"
	@echo "  make ui URL=...      真实 DOM 冒烟（ui_smoke_test.mjs）"
	@echo "  make llm             LLM 真实调用与成本计量"
	@echo "  make gate            四层全跑（需 URL）"
	@echo
	@echo "数据库迁移:"
	@echo "  make migrate         应用迁移（首次会自动 stamp 基线）"
	@echo "  make migrate-check   模型与迁移是否漂移（部署前跑）"
	@echo "  make revision M=...  生成新迁移（rev-id 自动编号，见 scripts/new_migration.sh）"
	@echo
	@echo "运维:"
	@echo "  make up / down / logs / ps"
	@echo "  make backup          备份 PostgreSQL + Redis"
	@echo "  make verify-backup   把最新备份还原到临时库比对行数"
	@echo "  make restore DIR=... 破坏性还原到线上库"
	@echo "  make dashboard       重新生成 Grafana 看板 JSON"

test:
	pytest -q

# The probe needs the public entry point. Override with URL=... on the CLI.
URL ?= https://124.222.190.3/support
probe:
	python scripts/acceptance_probe.py $(URL)

ui:
	bash scripts/ui_smoke.sh $(URL)
	bash scripts/ui_smoke.sh $(URL) scripts/ui_smoke_customer.mjs

llm:
	python scripts/llm_probe.py

# Everything. This is what "done" means for a change that touches the request
# path — not `make test`.
gate: test probe ui llm
	@echo
	@echo "四层验证全部通过。"

dashboard:
	python observability/grafana/build_dashboard.py
	@echo "Grafana 只在启动时读取 provisioning，需要重启才生效："
	@echo "  docker compose -f deployment/docker-compose.yml restart grafana"

COMPOSE = docker compose --env-file deployment/.env -f deployment/docker-compose.yml

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=200

ps:
	$(COMPOSE) ps

backup:
	bash scripts/backup.sh

verify-backup:
	bash scripts/verify_latest_backup.sh

restore:
	@test -n "$(DIR)" || (echo "用法: make restore DIR=backups/<timestamp>" && exit 2)
	bash scripts/restore_backup.sh --yes $(DIR)

migrate:
	bash scripts/migrate.sh upgrade

# Run this before deploying. It fails if a model changed without a migration,
# which is the silent drift that ``create_all`` used to hide.
migrate-check:
	bash scripts/migrate.sh check

revision:
	@test -n "$(M)" || (echo "用法: make revision M='add refund audit column' [REV_ID=0003_refund_audit]" && exit 2)
	bash scripts/new_migration.sh "$(M)"
