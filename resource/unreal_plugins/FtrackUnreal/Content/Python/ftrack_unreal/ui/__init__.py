# :coding: utf-8
# :copyright: Copyright (c) 2026 Mroya

'''Qt user interface for the integration.

The windows run in the editor's own process, on the game thread, with the Qt
event loop driven from Slate rather than from ``QApplication.exec()``. See
``qt_app`` for the mechanics.
'''
