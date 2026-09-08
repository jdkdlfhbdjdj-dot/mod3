import app as bot_app
from dashboard import dashboard_bp

bot_app.app.register_blueprint(dashboard_bp)

if __name__ == "__main__":
    bot_app.main()
