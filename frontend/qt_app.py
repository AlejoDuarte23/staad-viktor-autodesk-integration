from __future__ import annotations

import json
import sys

from PyQt6 import QtCore, QtGui, QtWidgets

from backend.staad_engine import collect_geometry_data
from backend.viktor_api import push_geometry_data_to_viktor

DEFAULT_URL = "https://beta.viktor.ai/workspaces/2539/app/editor/2262"


class WorkerSignals(QtCore.QObject):
    success = QtCore.pyqtSignal(dict)
    error = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal()


class PushWorker(QtCore.QRunnable):
    def __init__(self, viktor_url: str, token: str):
        super().__init__()
        self.viktor_url = viktor_url
        self.token = token
        self.signals = WorkerSignals()

    @QtCore.pyqtSlot()
    def run(self) -> None:
        try:
            geometry = collect_geometry_data()
            result = push_geometry_data_to_viktor(self.viktor_url, self.token, geometry)
        except Exception as exc:
            self.signals.error.emit(str(exc))
        else:
            self.signals.success.emit(result)
        finally:
            self.signals.finished.emit()


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("STAAD → VIKTOR Push")
        self.setMinimumSize(640, 480)

        self.thread_pool = QtCore.QThreadPool.globalInstance()

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(16)

        title = QtWidgets.QLabel("Push STAAD Geometry to VIKTOR")
        title.setFont(QtGui.QFont("Inter", 18, QtGui.QFont.Weight.Bold))
        layout.addWidget(title)

        subtitle = QtWidgets.QLabel(
            "Provide the VIKTOR workspace editor URL and API token. Press the button to"
            " collect geometry from the active STAAD session and upload it."
        )
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        form = QtWidgets.QFormLayout()
        form.setSpacing(12)
        layout.addLayout(form)

        self.url_input = QtWidgets.QLineEdit()
        self.url_input.setPlaceholderText("https://{env}.viktor.ai/workspaces/<id>/app/editor/<entity>")
        self.url_input.setText(DEFAULT_URL)
        form.addRow("VIKTOR editor URL", self.url_input)

        self.token_input = QtWidgets.QLineEdit()
        self.token_input.setPlaceholderText("Enter your VIKTOR token")
        self.token_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        form.addRow("API token", self.token_input)

        self.push_button = QtWidgets.QPushButton("Push STAAD data")
        self.push_button.clicked.connect(self.handle_submit)
        layout.addWidget(self.push_button)

        self.status_label = QtWidgets.QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.result_view = QtWidgets.QPlainTextEdit()
        self.result_view.setReadOnly(True)
        self.result_view.hide()
        layout.addWidget(self.result_view, 1)

    def handle_submit(self) -> None:
        viktor_url = self.url_input.text().strip()
        token = self.token_input.text().strip()

        if not viktor_url or not token:
            self.set_status("Please provide both the editor URL and API token.", "#d93025")
            return

        self.set_status(
            "Collecting geometry from STAAD and pushing to VIKTOR...", "#e67e22"
        )
        self.result_view.hide()
        self.push_button.setEnabled(False)

        worker = PushWorker(viktor_url, token)
        worker.signals.success.connect(self.on_success)
        worker.signals.error.connect(self.on_error)
        worker.signals.finished.connect(lambda: self.push_button.setEnabled(True))
        self.thread_pool.start(worker)

    def on_success(self, payload: dict) -> None:
        self.set_status("Geometry pushed successfully!", "#0f9d58")
        self.result_view.setPlainText(json.dumps(payload, indent=2))
        self.result_view.show()

    def on_error(self, message: str) -> None:
        self.set_status(message, "#d93025")

    def set_status(self, message: str, color: str) -> None:
        self.status_label.setText(message)
        self.status_label.setStyleSheet(f"color: {color}; font-weight: 600;")


def run() -> int:
    app = QtWidgets.QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())
