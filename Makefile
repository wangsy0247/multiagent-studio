.PHONY: setup run stop help

help: ## 显示可用命令
	@echo "MultiAgent Studio"
	@echo ""
	@echo "  make setup   首次安装 (幂等: 创建 conda 环境、生成 .env、装依赖)"
	@echo "  make run     启动全部服务 (Harness 8001 / App 8000 / Frontend 3000)"
	@echo "  make stop    停止所有服务"
	@echo ""

setup: ## 首次安装/环境检查 (幂等)
	./setup.sh

run: ## 启动全部服务 (前台, Ctrl+C 停止)
	./start.sh

stop: ## 停止所有服务
	@if [ -f /tmp/multiagent_studio.pids ]; then \
		kill $$(cat /tmp/multiagent_studio.pids) 2>/dev/null && echo "已停止"; \
		rm -f /tmp/multiagent_studio.pids; \
	else \
		echo "未找到运行中的服务 (无 /tmp/multiagent_studio.pids)"; \
	fi
