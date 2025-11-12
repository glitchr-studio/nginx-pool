nginx:
	@curl -fsSL https://nginx.org/keys/nginx_signing.key   | sudo gpg --dearmor -o /usr/share/keyrings/nginx-keyring.gpg
	@echo -e "deb [signed-by=/usr/share/keyrings/nginx-keyring.gpg]\n http://nginx.org/packages/mainline/debian bookworm nginx" | tee /etc/apt/sources.list.d/nginx-mainline.list
	@apt install nginx
	@cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.bak || true
	@cp nginx.conf /etc/nginx/

conf:
	@cp conf.d/* /etc/nginx/conf.d
	@nginx -t

reload:
	@systemctl restart nginx
