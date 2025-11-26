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
build-config:
	@sed -e "s/{{DOMAIN}}/$(DOMAIN_ESC)/g" \
		 -e "s/{{DOMAIN_ESC}}/$(DOMAIN_ESC2)/g" \
		 nginx.conf.template > nginx.conf
	@sed -e "s/{{DOMAIN}}/$(DOMAIN_ESC)/g" \
	 	 -e "s/{{DOMAIN_ESC}}/$(DOMAIN_ESC2)/g" \
	 	 conf.d/pool.conf.template > conf.d/pool.conf

nginx:
	@curl -fsSL https://nginx.org/keys/nginx_signing.key   | sudo gpg --dearmor -o /usr/share/keyrings/nginx-keyring.gpg
	@echo -e "deb [signed-by=/usr/share/keyrings/nginx-keyring.gpg]\n http://nginx.org/packages/mainline/debian bookworm nginx" | tee /etc/apt/sources.list.d/nginx-mainline.list
	@apt install nginx python3-certbot-nginx
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

conf: build-config
	@cp bin/* /usr/local/bin/
	@cp conf.d/* /etc/nginx/conf.d
	@touch /etc/nginx/users.map /etc/nginx/legacy.map /etc/nginx/ports_http.map /etc/nginx/ports_https.map
	@nginx -t
	@systemctl restart nginx

reload:
	@systemctl restart nginx
