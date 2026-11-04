from pathlib import Path


NGINX_CONFIG = Path(__file__).resolve().parents[1] / "deploy" / "nginx.conf"


def test_nginx_root_redirect_is_relative_and_other_routes_remain_configured():
    config = NGINX_CONFIG.read_text(encoding="utf-8")

    assert "listen 80;" in config
    assert "absolute_redirect off;" in config
    assert "location = / {\n        return 302 /submit;\n    }" in config
    assert config.index("location = / {") < config.index("location / {")
    assert "location /static/" in config
    assert "alias /app/staticfiles/;" in config
    assert "client_max_body_size 40m;" in config
    assert "client_body_timeout 300s;" in config
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in config
    assert "proxy_connect_timeout 30s;" in config
    assert "proxy_send_timeout 300s;" in config
    assert "proxy_read_timeout 300s;" in config
