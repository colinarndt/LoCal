"""Bundle entry point.

PyInstaller runs its entry script as `__main__`, with no parent package -- so
pointing it straight at `social_calendar/app.py` makes that module's relative
imports (`from . import db, ...`) fail at launch. Importing the package here
instead means the app runs inside `social_calendar` exactly as it does under
`python -m social_calendar.app`.
"""

from social_calendar.app import main

if __name__ == "__main__":
    main()
