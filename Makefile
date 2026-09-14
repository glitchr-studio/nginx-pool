# ------------------------------------------------------
# Variables
# ------------------------------------------------------
ifneq ("$(wildcard .env)","")
	include .env
	export
endif

DOMAIN ?= localhost
DOMAIN_ESC := $(subst .,\.,$(DOMAIN))
DOMAIN_ESC2 := $(subst .,\\\.,$(DOMAIN))

# ------------------------------------------------------
# Template replacement task
# ------------------------------------------------------
config:
	@sed -e "s/{{DOMAIN}}/$(DOMAIN_ESC)/g" \
		 -e "s/{{DOMAIN_ESC}}/$(DOMAIN_ESC2)/g" \
		 nginx.conf.template > nginx.conf
	@sed -e "s/{{DOMAIN}}/$(DOMAIN_ESC)/g" \
	 	 -e "s/{{DOMAIN_ESC}}/$(DOMAIN_ESC2)/g" \
	 	 conf.d/pool.conf.template > conf.d/pool.conf
	@cp conf.d/00-gitlab.conf.template conf.d/00-gitlab.conf

nginx:
	@curl -fsSL https://nginx.org/keys/nginx_signing.key   | sudo gpg --dearmor -o /usr/share/keyrings/nginx-keyring.gpg
	@echo -e "deb [signed-by=/usr/share/keyrings/nginx-keyring.gpg]\n http://nginx.org/packages/mainline/debian bookworm nginx" | tee /etc/apt/sources.list.d/nginx-mainline.list
	@apt install nginx python3-certbot-nginx nginx-extras libpam0g-dev
	@cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.bak || true
	@cp nginx.conf /etc/nginx/

php: 
	@apt install ca-certificates apt-transport-https software-properties-common
	@curl -fsSL https://packages.sury.org/php/README.txt | bash -x
	@add-apt-repository ppa:ondrej/php
	@apt update
	@apt install php8.3 php8.3-fpm php8.3-intl php8.3-imagick php8.3-mysql php8.3-xml php8.3-curl php8.3-mbstring \
	             php8.3-bcmath php8.3-zip php8.3-sqlite3 php8.3-gd php8.3-opcache php3-igbinary php8.3-cli php8.3-igbinary
	@update-alternatives --config php

install: config automation
	@cp bin/* /usr/local/bin/
	@cp nginx.conf /etc/nginx/
	@cp -n noindex.map.dist /etc/nginx/noindex.map 2>/dev/null || true
	@cp conf.d/*.conf /etc/nginx/conf.d/
	@touch /etc/nginx/user.map /etc/nginx/legacy.map /etc/nginx/domain.map /etc/nginx/lock.map /etc/nginx/ssl_cert.map /etc/nginx/noindex.map
	@touch /etc/nginx/port_http.map /etc/nginx/port_https.map
	@# Keepalive upstreams for the Docker vhosts. pool.conf includes the
	@# directory with a glob (harmless when empty) but the two maps are plain
	@# `include`s inside map blocks, where a missing file is fatal - so they
	@# must exist before the first `nginx-pool ports` run fills them in.
	@install -d /etc/nginx/upstreams.d
	@touch /etc/nginx/upstream_http.map /etc/nginx/upstream_https.map
	@/usr/local/bin/nginx-pool logs
	@$(MAKE) reload

# Restart policy for the daemon + the pool's scheduled jobs.
# The cron entries used to live in root's personal crontab; if they are still
# there they would run twice, so this refuses to install until they are gone.
automation:
	@if crontab -l 2>/dev/null | grep -q "nginx-pool"; then \
		echo "[!] root's crontab still has nginx-pool entries."; \
		echo "    Remove them first (crontab -e), they are superseded by /etc/cron.d/nginx-pool:"; \
		crontab -l 2>/dev/null | grep "nginx-pool" | sed 's/^/      /'; \
		exit 1; \
	fi
	@install -d /etc/systemd/system/nginx.service.d
	@install -m 644 systemd/override.conf /etc/systemd/system/nginx.service.d/override.conf
	@install -m 644 cron/nginx-pool /etc/cron.d/nginx-pool
	@systemctl daemon-reload
	@echo "[ok] restart policy + /etc/cron.d/nginx-pool installed"

reload:
	@nginx -t
	@systemctl restart nginx
