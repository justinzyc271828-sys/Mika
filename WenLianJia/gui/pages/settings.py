"""Settings: model, costs, typography, watermark, Netdisk and data paths."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.latex_utils import find_xelatex, xelatex_version
from core.logging_setup import configure_from_settings
from gui.context import AppContext
from gui.table_utils import prepare_table
from gui.widgets import PageHeader, label, set_variant
from gui.workers import ConnectionWorker, OAuthWorker


def _group(title: str) -> tuple[QGroupBox, QFormLayout]:
    box = QGroupBox(title)
    form = QFormLayout(box)
    form.setSpacing(9)
    box.setLayout(form)
    return box, form


class SettingsPage(QWidget):
    saved = Signal()

    def __init__(self, context: AppContext, parent: QWidget | None = None):
        super().__init__(parent)
        self.context = context
        self._oauth_worker: OAuthWorker | None = None
        self._check_worker: ConnectionWorker | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 22, 26, 18)
        layout.setSpacing(14)
        layout.addWidget(
            PageHeader(
                "设置",
                "密钥用 Windows DPAPI 加密，不写入日志或仓库。模型价格必须由你填写；留空时只记录 token，不估算金额。",
            )
        )

        tabs = QTabWidget()
        tabs.addTab(self._model_tab(), "模型与费用")
        tabs.addTab(self._research_tab(), "检索与书目")
        tabs.addTab(self._typesetting_tab(), "排版与水印")
        tabs.addTab(self._netdisk_tab(), "百度网盘")
        tabs.addTab(self._data_tab(), "数据与日志")
        layout.addWidget(tabs, 1)

        bottom = QHBoxLayout()
        self.status_label = label("", "Hint", True)
        bottom.addWidget(self.status_label, 1)
        save = set_variant(QPushButton("保存设置"), "primary")
        save.setMinimumWidth(150)
        save.clicked.connect(self._save)
        bottom.addWidget(save)
        layout.addLayout(bottom)

        self._load()

    # ------------------------------------------------------------------ tabs
    def _model_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(
            label(
                "API Key 只写入本机加密存储。模型调用失败会保留错误并停止该题，不会把失败包装成答案。",
                "Hint",
                True,
            )
        )

        api_box, api_form = _group("OpenAI 凭据")
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("留空表示不修改")
        self.api_key_status = label("", "Hint")
        clear_key = QPushButton("清除")
        clear_key.clicked.connect(lambda: self._clear_secret("openai_api_key", self.api_key_status))
        key_row = QHBoxLayout()
        key_row.addWidget(self.api_key_edit, 1)
        key_row.addWidget(clear_key)
        api_form.addRow("API Key", key_row)
        api_form.addRow("", self.api_key_status)
        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("默认官方地址；兼容网关可填完整 /v1 地址")
        api_form.addRow("Base URL", self.base_url_edit)
        layout.addWidget(api_box)

        model_box, model_form = _group("模型与并发")
        self.model_edit = QLineEdit()
        self.review_model_edit = QLineEdit()
        self.review_model_edit.setPlaceholderText("留空则使用主模型")
        self.escalation_edit = QLineEdit()
        self.escalation_edit.setPlaceholderText("可选；需同时开启“自动升级”")
        self.auto_escalate_check = QCheckBox("复核失败时自动升级模型并强制人工复核")
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, 12)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(30, 1800)
        self.timeout_spin.setSuffix(" 秒")
        self.max_tokens_spin = QSpinBox()
        self.max_tokens_spin.setRange(500, 30000)
        self.temperature_spin = QDoubleSpinBox()
        self.temperature_spin.setRange(0.0, 1.5)
        self.temperature_spin.setSingleStep(0.1)
        model_form.addRow("主模型", self.model_edit)
        model_form.addRow("复核模型", self.review_model_edit)
        model_form.addRow("升级模型", self.escalation_edit)
        model_form.addRow("", self.auto_escalate_check)
        model_form.addRow("并发生成", self.concurrency_spin)
        model_form.addRow("单次超时", self.timeout_spin)
        model_form.addRow("最大输出 token", self.max_tokens_spin)
        model_form.addRow("温度", self.temperature_spin)
        layout.addWidget(model_box)

        budget_box, budget_form = _group("费用与预算")
        self.currency_combo = QComboBox()
        self.currency_combo.addItem("美元 USD", "USD")
        self.currency_combo.addItem("人民币 CNY", "CNY")
        self.enforce_check = QCheckBox("启用预算硬限制（未知价格时阻止继续）")
        self.per_book_spin = QDoubleSpinBox()
        self.per_book_spin.setRange(0, 100000)
        self.per_book_spin.setDecimals(2)
        self.per_book_spin.setPrefix("预算 ")
        self.daily_spin = QDoubleSpinBox()
        self.daily_spin.setRange(0, 100000)
        self.daily_spin.setDecimals(2)
        self.daily_spin.setPrefix("预算 ")
        budget_form.addRow("币种", self.currency_combo)
        budget_form.addRow("", self.enforce_check)
        budget_form.addRow("单本上限", self.per_book_spin)
        budget_form.addRow("每日上限", self.daily_spin)
        budget_form.addRow(
            "价格表",
            label("每百万 token 的单价，必须按你的实际账单填写；0 或空表示未配置。", "Hint", True),
        )
        self.price_table = QTableWidget()
        prepare_table(self.price_table, ["模型 ID", "输入 / 百万 token", "输出 / 百万 token"])
        self.price_table.setMinimumHeight(150)
        self.price_table.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
        budget_form.addRow(self.price_table)
        price_buttons = QHBoxLayout()
        add_price = QPushButton("添加一行")
        remove_price = QPushButton("删除选中")
        add_price.clicked.connect(lambda: self.price_table.insertRow(self.price_table.rowCount()))
        remove_price.clicked.connect(self._remove_price_row)
        price_buttons.addWidget(add_price)
        price_buttons.addWidget(remove_price)
        price_buttons.addStretch(1)
        budget_form.addRow(price_buttons)
        layout.addWidget(budget_box)

        image_box, image_form = _group("图片 API（预留，当前版本不调用）")
        self.image_enabled_check = QCheckBox("启用图片 API")
        self.image_base_edit = QLineEdit()
        self.image_model_edit = QLineEdit()
        self.image_key_edit = QLineEdit()
        self.image_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.image_key_edit.setPlaceholderText("留空表示不修改")
        image_form.addRow("", self.image_enabled_check)
        image_form.addRow("Base URL", self.image_base_edit)
        image_form.addRow("模型", self.image_model_edit)
        image_form.addRow("API Key", self.image_key_edit)
        image_form.addRow(
            "",
            label(
                "当前商品图由本地确定性设计器生成，不会把模板图冒充 AI 生成。此区块仅保存后续接入所需配置。",
                "Hint",
                True,
            ),
        )
        layout.addWidget(image_box)
        layout.addStretch(1)
        return self._scroll(page)

    def _research_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(
            label(
                "DeepSeek Search/Fetch 使用独立的 DeepSeek Key，不会发送到 OpenAI。"
                "搜索与抓取按成功请求计费，价格必须按你的实际账单填写；留 0 表示不估算金额。",
                "Hint",
                True,
            )
        )

        key_box, key_form = _group("DeepSeek 凭据")
        self.deepseek_key_edit = QLineEdit()
        self.deepseek_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.deepseek_key_edit.setPlaceholderText("留空表示不修改")
        self.deepseek_key_status = label("", "Hint")
        deepseek_clear = QPushButton("清除")
        deepseek_clear.clicked.connect(
            lambda: self._clear_secret("deepseek_api_key", self.deepseek_key_status)
        )
        deepseek_row = QHBoxLayout()
        deepseek_row.addWidget(self.deepseek_key_edit, 1)
        deepseek_row.addWidget(deepseek_clear)
        key_form.addRow("API Key", deepseek_row)
        key_form.addRow("", self.deepseek_key_status)
        self.deepseek_enabled_check = QCheckBox("启用联网书目检索（关闭时只允许手工填写）")
        self.deepseek_base_edit = QLineEdit()
        self.deepseek_base_edit.setPlaceholderText("https://api.deepseek.com")
        key_form.addRow("", self.deepseek_enabled_check)
        key_form.addRow("Base URL", self.deepseek_base_edit)
        layout.addWidget(key_box)

        cost_box, cost_form = _group("搜索与抓取费用（美元）")
        self.deepseek_search_spin = QDoubleSpinBox()
        self.deepseek_search_spin.setRange(0, 100)
        self.deepseek_search_spin.setDecimals(4)
        self.deepseek_search_spin.setPrefix("$ ")
        self.deepseek_search_spin.setSuffix(" / 1000 次")
        self.deepseek_fetch_spin = QDoubleSpinBox()
        self.deepseek_fetch_spin.setRange(0, 100)
        self.deepseek_fetch_spin.setDecimals(4)
        self.deepseek_fetch_spin.setPrefix("$ ")
        self.deepseek_fetch_spin.setSuffix(" / 1000 次")
        self.deepseek_count_spin = QSpinBox()
        self.deepseek_count_spin.setRange(1, 50)
        self.deepseek_cache_spin = QSpinBox()
        self.deepseek_cache_spin.setRange(0, 365)
        self.deepseek_cache_spin.setSuffix(" 天")
        cost_form.addRow("Search 单价", self.deepseek_search_spin)
        cost_form.addRow("Fetch 单价", self.deepseek_fetch_spin)
        cost_form.addRow("每次搜索结果数", self.deepseek_count_spin)
        cost_form.addRow("书目缓存", self.deepseek_cache_spin)
        layout.addWidget(cost_box)

        book_box, book_form = _group("书目与封面")
        self.books_auto_check = QCheckBox("生产前自动检索（未配置 Key 时跳过并提示）")
        self.books_require_cover_check = QCheckBox("交付必须使用已核验原版封面（不允许占位封面）")
        self.books_similarity_spin = QDoubleSpinBox()
        self.books_similarity_spin.setRange(0.0, 1.0)
        self.books_similarity_spin.setSingleStep(0.05)
        book_form.addRow("", self.books_auto_check)
        book_form.addRow("", self.books_require_cover_check)
        book_form.addRow("书名匹配阈值", self.books_similarity_spin)
        book_form.addRow(
            "",
            label(
                "封面只从可信书目网页或你手工选择的文件读取；无法确认版本时标记待人工核对，"
                "不会用相似封面冒充原版。",
                "Hint",
                True,
            ),
        )
        layout.addWidget(book_box)
        layout.addStretch(1)
        return self._scroll(page)

    def _typesetting_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        water_box, water_form = _group("斜向品牌水印")
        self.water_text_edit = QLineEdit()
        self.water_font_edit = QLineEdit()
        font_browse = QPushButton("选择字体…")
        font_browse.clicked.connect(self._choose_font)
        font_row = QHBoxLayout()
        font_row.addWidget(self.water_font_edit, 1)
        font_row.addWidget(font_browse)
        self.water_size_spin = QSpinBox()
        self.water_size_spin.setRange(12, 200)
        self.water_color_edit = QLineEdit()
        self.water_opacity_spin = QSpinBox()
        self.water_opacity_spin.setRange(1, 80)
        self.water_opacity_spin.setSuffix(" %")
        self.water_rotation_spin = QSpinBox()
        self.water_rotation_spin.setRange(0, 90)
        self.water_rotation_spin.setSuffix("°")
        self.water_position_combo = QComboBox()
        self.water_position_combo.addItem("页面中心", "center")
        water_form.addRow("水印文字", self.water_text_edit)
        water_form.addRow("中文字体", font_row)
        water_form.addRow("字号", self.water_size_spin)
        water_form.addRow("颜色", self.water_color_edit)
        water_form.addRow("不透明度", self.water_opacity_spin)
        water_form.addRow("角度", self.water_rotation_spin)
        water_form.addRow("位置", self.water_position_combo)
        water_form.addRow("", label("水印逐页叠加在最终 PDF 上；原始 PDF 不会被覆盖。", "Hint", True))
        layout.addWidget(water_box)

        latex_box, latex_form = _group("XeLaTeX 排版")
        self.xelatex_edit = QLineEdit()
        self.xelatex_edit.setPlaceholderText("留空自动探测")
        detect = QPushButton("自动探测")
        detect.clicked.connect(self._detect_latex)
        xelatex_row = QHBoxLayout()
        xelatex_row.addWidget(self.xelatex_edit, 1)
        xelatex_row.addWidget(detect)
        self.latex_status = label("", "Hint")
        self.font_main_edit = QLineEdit()
        self.font_latin_edit = QLineEdit()
        self.margin_spin = QSpinBox()
        self.margin_spin.setRange(12, 45)
        self.margin_spin.setSuffix(" mm")
        latex_form.addRow("xelatex 路径", xelatex_row)
        latex_form.addRow("", self.latex_status)
        latex_form.addRow("中文主字体", self.font_main_edit)
        latex_form.addRow("西文字体", self.font_latin_edit)
        latex_form.addRow("页边距", self.margin_spin)
        layout.addWidget(latex_box)
        layout.addStretch(1)
        return self._scroll(page)

    def _netdisk_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(
            label(
                "上传接口只允许应用目录 /apps/{应用名} 下的路径。未购买“文件分享服务”时，创建分享会明确失败，不会显示成成功。",
                "Hint",
                True,
            )
        )

        app_box, app_form = _group("百度网盘应用")
        self.baidu_key_edit = QLineEdit()
        self.baidu_secret_edit = QLineEdit()
        self.baidu_secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.baidu_secret_edit.setPlaceholderText("留空表示不修改")
        self.baidu_name_edit = QLineEdit()
        self.baidu_name_edit.setPlaceholderText("App 名称，用于 /apps/{appname} 路径")
        self.baidu_appid_edit = QLineEdit()
        self.baidu_appid_edit.setPlaceholderText("应用 ID；分享接口使用，可留空回退 AppKey")
        self.netdisk_root_edit = QLineEdit()
        self.netdisk_redirect_edit = QLineEdit()
        self.netdisk_redirect_edit.setPlaceholderText("oob 或控制台登记的回调地址")
        self.baidu_status = label("", "Hint", True)
        app_form.addRow("AppKey", self.baidu_key_edit)
        app_form.addRow("SecretKey", self.baidu_secret_edit)
        app_form.addRow("App 名称", self.baidu_name_edit)
        app_form.addRow("App ID", self.baidu_appid_edit)
        app_form.addRow("商品库根目录", self.netdisk_root_edit)
        app_form.addRow("授权回调", self.netdisk_redirect_edit)
        app_form.addRow("状态", self.baidu_status)
        layout.addWidget(app_box)

        oauth_box, oauth_form = _group("授权")
        self.oauth_url_edit = QLineEdit()
        self.oauth_url_edit.setReadOnly(True)
        self.oauth_url_edit.setPlaceholderText("保存应用信息后获取授权链接")
        open_auth = QPushButton("打开授权页")
        open_auth.clicked.connect(self._open_auth_url)
        get_auth = QPushButton("生成授权链接")
        get_auth.clicked.connect(self._make_auth_url)
        code_button = QPushButton("粘贴授权码…")
        code_button.clicked.connect(self._exchange_code)
        check_auth = QPushButton("检查授权状态")
        check_auth.clicked.connect(self._check_netdisk)
        oauth_form.addRow("授权链接", self.oauth_url_edit)
        row = QHBoxLayout()
        for button in (get_auth, open_auth, code_button, check_auth):
            row.addWidget(button)
        row.addStretch(1)
        oauth_form.addRow(row)
        layout.addWidget(oauth_box)

        upload_box, upload_form = _group("上传与分享")
        self.auto_upload_check = QCheckBox("新商品默认上传")
        self.auto_share_check = QCheckBox("上传后默认创建分享")
        self.share_period_spin = QSpinBox()
        self.share_period_spin.setRange(0, 3650)
        self.share_period_spin.setSuffix(" 天（0 为永久）")
        self.share_password_edit = QLineEdit()
        self.share_password_edit.setPlaceholderText("4 位数字或小写字母；留空随机")
        self.verify_hash_check = QCheckBox("下载云端文件并校验 SHA-256（较慢，但能证明内容一致）")
        self.include_source_check = QCheckBox("上传原始教材（默认不上传，注意版权）")
        upload_form.addRow("", self.auto_upload_check)
        upload_form.addRow("", self.auto_share_check)
        upload_form.addRow("分享有效期", self.share_period_spin)
        upload_form.addRow("分享密码", self.share_password_edit)
        upload_form.addRow("", self.verify_hash_check)
        upload_form.addRow("", self.include_source_check)
        upload_form.addRow(
            "",
            label(
                "云端列表返回的 md5 不是本地文件 MD5，不能用来证明内容一致；只有开启下载校验才会记录真实哈希证据。",
                "Hint",
                True,
            ),
        )
        layout.addWidget(upload_box)
        layout.addStretch(1)
        return self._scroll(page)

    def _data_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        paths_box, paths_form = _group("数据目录")
        self.root_edit = QLineEdit(str(self.context.paths.root))
        self.root_edit.setReadOnly(True)
        open_root = QPushButton("打开数据目录")
        open_root.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.context.paths.root))))
        open_logs = QPushButton("打开日志目录")
        open_logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.context.paths.log_dir))))
        row = QHBoxLayout()
        row.addWidget(self.root_edit, 1)
        row.addWidget(open_root)
        paths_form.addRow("数据根目录", row)
        paths_form.addRow("数据库", label(str(self.context.paths.database), "Hint", True))
        paths_form.addRow("配置文件", label(str(self.context.paths.settings), "Hint", True))
        paths_form.addRow("密钥文件", label(str(self.context.paths.secrets), "Hint", True))
        paths_form.addRow("", open_logs)
        paths_form.addRow(
            "",
            label(
                "可通过环境变量 WLJ_DATA_DIR 在启动前切换数据根目录。移动数据目录前请先关闭程序。",
                "Hint",
                True,
            ),
        )
        layout.addWidget(paths_box)

        log_box, log_form = _group("日志")
        self.log_level_combo = QComboBox()
        for value in ("DEBUG", "INFO", "WARNING", "ERROR"):
            self.log_level_combo.addItem(value, value)
        log_form.addRow("级别", self.log_level_combo)
        log_form.addRow(
            "",
            label(
                "日志会自动脱敏 sk-、Bearer、access_token 和 refresh_token。不要手工把密钥写入备注或商品信息。",
                "Hint",
                True,
            ),
        )
        layout.addWidget(log_box)

        about_box, about_form = _group("版本")
        about_form.addRow("文链家 WenLianJia", label(f"v{self.context.settings.get('app.version', '1.0.0')}", "Hint"))
        about_form.addRow(
            "",
            label(
                "本工具用于整理已合法获得的教材习题。解析作品仅供学习参考；未经许可不得复制、销售原教材内容。",
                "Hint",
                True,
            ),
        )
        layout.addWidget(about_box)
        layout.addStretch(1)
        return self._scroll(page)

    @staticmethod
    def _scroll(page: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    # ------------------------------------------------------------------ load
    def _load(self) -> None:
        s = self.context.settings
        self.api_key_status.setText(
            "已保存（留空不修改）" if self.context.secrets.has("openai_api_key") else "尚未保存"
        )
        self.deepseek_key_status.setText(
            "已保存（留空不修改）" if self.context.secrets.has("deepseek_api_key") else "尚未保存"
        )
        self.deepseek_enabled_check.setChecked(bool(s.get("deepseek.enabled", False)))
        self.deepseek_base_edit.setText(str(s.get("deepseek.base_url", "https://api.deepseek.com")))
        self.deepseek_search_spin.setValue(float(s.get("deepseek.search_price_per_1000", 0.75)))
        self.deepseek_fetch_spin.setValue(float(s.get("deepseek.fetch_price_per_1000", 0.75)))
        self.deepseek_count_spin.setValue(int(s.get("deepseek.search_count", 10)))
        self.deepseek_cache_spin.setValue(int(s.get("deepseek.cache_days", 30)))
        self.books_auto_check.setChecked(bool(s.get("books.auto_lookup", True)))
        self.books_require_cover_check.setChecked(bool(s.get("books.require_verified_cover", False)))
        self.books_similarity_spin.setValue(float(s.get("books.title_similarity", 0.6)))
        self.base_url_edit.setText(str(s.get("openai.base_url", "")))
        self.model_edit.setText(str(s.get("openai.model", "")))
        self.review_model_edit.setText(str(s.get("openai.review_model", "")))
        self.escalation_edit.setText(str(s.get("openai.escalation_model", "")))
        self.auto_escalate_check.setChecked(bool(s.get("openai.auto_escalate", False)))
        self.concurrency_spin.setValue(int(s.get("openai.max_concurrency", 4)))
        self.timeout_spin.setValue(int(s.get("openai.timeout_seconds", 240)))
        self.max_tokens_spin.setValue(int(s.get("openai.max_output_tokens", 4500)))
        self.temperature_spin.setValue(float(s.get("openai.temperature", 0.2)))

        currency = str(s.get("budget.currency", "USD"))
        index = self.currency_combo.findData(currency)
        self.currency_combo.setCurrentIndex(index if index >= 0 else 0)
        self.enforce_check.setChecked(bool(s.get("budget.enforce", False)))
        self.per_book_spin.setValue(float(s.get("budget.per_book", 0)))
        self.daily_spin.setValue(float(s.get("budget.daily", 0)))
        self._load_prices(s.get("budget.prices", {}) or {})

        self.image_enabled_check.setChecked(bool(s.get("image_api.enabled", False)))
        self.image_base_edit.setText(str(s.get("image_api.base_url", "")))
        self.image_model_edit.setText(str(s.get("image_api.model", "")))

        self.water_text_edit.setText(str(s.get("watermark.text", "")))
        self.water_font_edit.setText(str(s.get("watermark.font", "")))
        self.water_size_spin.setValue(int(s.get("watermark.font_size", 42)))
        self.water_color_edit.setText(str(s.get("watermark.color", "#9AA0A6")))
        self.water_opacity_spin.setValue(round(float(s.get("watermark.opacity", 0.18)) * 100))
        self.water_rotation_spin.setValue(int(s.get("watermark.rotation", 35)))
        position = str(s.get("watermark.position", "center"))
        position_index = self.water_position_combo.findData(position)
        self.water_position_combo.setCurrentIndex(position_index if position_index >= 0 else 0)

        self.xelatex_edit.setText(str(s.get("latex.xelatex_path", "")))
        self.font_main_edit.setText(str(s.get("latex.font_main", "")))
        self.font_latin_edit.setText(str(s.get("latex.font_latin", "")))
        self.margin_spin.setValue(int(s.get("latex.margin_mm", 22)))
        self._update_latex_status()

        self.baidu_key_edit.setText(str(s.get("netdisk.app_key", "")))
        self.baidu_name_edit.setText(str(s.get("netdisk.app_name", "")))
        self.baidu_appid_edit.setText(str(s.get("netdisk.app_id", "")))
        self.netdisk_root_edit.setText(str(s.get("netdisk.root_dir", "")))
        self.netdisk_redirect_edit.setText(str(s.get("netdisk.redirect_uri", "oob")))
        self.auto_upload_check.setChecked(bool(s.get("netdisk.auto_upload", False)))
        self.auto_share_check.setChecked(bool(s.get("netdisk.auto_share", False)))
        self.share_period_spin.setValue(int(s.get("netdisk.share_period", 0)))
        self.share_password_edit.setText(str(s.get("netdisk.share_password", "")))
        self.verify_hash_check.setChecked(bool(s.get("netdisk.verify_download_hash", False)))
        self.include_source_check.setChecked(bool(s.get("netdisk.include_source", False)))
        self._update_netdisk_status()

        self.log_level_combo.setCurrentIndex(max(0, self.log_level_combo.findData(str(s.get("logging.level", "INFO")))))

    def _load_prices(self, prices: dict) -> None:
        self.price_table.setRowCount(0)
        for model, entry in prices.items():
            if not isinstance(entry, dict):
                continue
            row = self.price_table.rowCount()
            self.price_table.insertRow(row)
            self.price_table.setItem(row, 0, QTableWidgetItem(str(model)))
            self.price_table.setItem(row, 1, QTableWidgetItem(str(entry.get("input_per_million", ""))))
            self.price_table.setItem(row, 2, QTableWidgetItem(str(entry.get("output_per_million", ""))))

    def _remove_price_row(self) -> None:
        row = self.price_table.currentRow()
        if row >= 0:
            self.price_table.removeRow(row)

    # ------------------------------------------------------------------ save
    def _save(self, *, notify: bool = True) -> bool:
        try:
            prices = self._read_prices()
            api_key = self.api_key_edit.text().strip()
            image_key = self.image_key_edit.text().strip()
            baidu_secret = self.baidu_secret_edit.text().strip()
            deepseek_key = self.deepseek_key_edit.text().strip()
            if api_key:
                self.context.secrets.set("openai_api_key", api_key)
            if image_key:
                self.context.secrets.set("image_api_key", image_key)
            if baidu_secret:
                self.context.secrets.set("baidu_app_secret", baidu_secret)
            if deepseek_key:
                self.context.secrets.set("deepseek_api_key", deepseek_key)

            self.context.settings.update(
                {
                    "openai": {
                        "base_url": self.base_url_edit.text().strip(),
                        "model": self.model_edit.text().strip(),
                        "review_model": self.review_model_edit.text().strip(),
                        "escalation_model": self.escalation_edit.text().strip(),
                        "auto_escalate": self.auto_escalate_check.isChecked(),
                        "max_concurrency": self.concurrency_spin.value(),
                        "timeout_seconds": self.timeout_spin.value(),
                        "max_output_tokens": self.max_tokens_spin.value(),
                        "temperature": self.temperature_spin.value(),
                    },
                    "budget": {
                        "currency": self.currency_combo.currentData(),
                        "enforce": self.enforce_check.isChecked(),
                        "per_book": self.per_book_spin.value(),
                        "daily": self.daily_spin.value(),
                        "prices": prices,
                    },
                    "image_api": {
                        "enabled": self.image_enabled_check.isChecked(),
                        "base_url": self.image_base_edit.text().strip(),
                        "model": self.image_model_edit.text().strip(),
                    },
                    "deepseek": {
                        "enabled": self.deepseek_enabled_check.isChecked(),
                        "base_url": self.deepseek_base_edit.text().strip() or "https://api.deepseek.com",
                        "search_price_per_1000": self.deepseek_search_spin.value(),
                        "fetch_price_per_1000": self.deepseek_fetch_spin.value(),
                        "search_count": self.deepseek_count_spin.value(),
                        "cache_days": self.deepseek_cache_spin.value(),
                    },
                    "books": {
                        "auto_lookup": self.books_auto_check.isChecked(),
                        "require_verified_cover": self.books_require_cover_check.isChecked(),
                        "title_similarity": self.books_similarity_spin.value(),
                    },
                    "watermark": {
                        "text": self.water_text_edit.text().strip(),
                        "font": self.water_font_edit.text().strip(),
                        "font_size": self.water_size_spin.value(),
                        "color": self.water_color_edit.text().strip(),
                        "opacity": self.water_opacity_spin.value() / 100.0,
                        "rotation": self.water_rotation_spin.value(),
                        "position": self.water_position_combo.currentData(),
                    },
                    "latex": {
                        "xelatex_path": self.xelatex_edit.text().strip(),
                        "font_main": self.font_main_edit.text().strip(),
                        "font_latin": self.font_latin_edit.text().strip(),
                        "margin_mm": self.margin_spin.value(),
                    },
                    "netdisk": {
                        "app_key": self.baidu_key_edit.text().strip(),
                        "app_name": self.baidu_name_edit.text().strip(),
                        "app_id": self.baidu_appid_edit.text().strip(),
                        "root_dir": self.netdisk_root_edit.text().strip(),
                        "redirect_uri": self.netdisk_redirect_edit.text().strip() or "oob",
                        "auto_upload": self.auto_upload_check.isChecked(),
                        "auto_share": self.auto_share_check.isChecked(),
                        "share_period": self.share_period_spin.value(),
                        "share_password": self.share_password_edit.text().strip(),
                        "verify_download_hash": self.verify_hash_check.isChecked(),
                        "include_source": self.include_source_check.isChecked(),
                    },
                    "logging": {"level": self.log_level_combo.currentData()},
                }
            )
            configure_from_settings(self.context.paths, self.context.settings, console=False)
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"保存失败：{type(exc).__name__}: {exc}")
            if notify:
                QMessageBox.critical(self, "保存失败", f"{type(exc).__name__}: {exc}")
            return False
        self.api_key_edit.clear()
        self.image_key_edit.clear()
        self.baidu_secret_edit.clear()
        self.deepseek_key_edit.clear()
        self.api_key_status.setText(
            "已保存（留空不修改）" if self.context.secrets.has("openai_api_key") else "尚未保存"
        )
        self.deepseek_key_status.setText(
            "已保存（留空不修改）" if self.context.secrets.has("deepseek_api_key") else "尚未保存"
        )
        self._update_netdisk_status()
        self._update_latex_status()
        self.status_label.setText("设置已保存。")
        self.saved.emit()
        if notify:
            QMessageBox.information(self, "已保存", "设置已保存。密钥未写入配置文件或日志。")
        return True

    def _read_prices(self) -> dict:
        prices: dict[str, dict] = {}
        for row in range(self.price_table.rowCount()):
            model_item = self.price_table.item(row, 0)
            input_item = self.price_table.item(row, 1)
            output_item = self.price_table.item(row, 2)
            model = (model_item.text() if model_item else "").strip()
            raw_in = (input_item.text() if input_item else "").strip()
            raw_out = (output_item.text() if output_item else "").strip()
            if not model and not raw_in and not raw_out:
                continue
            if not model:
                raise ValueError(f"价格表第 {row + 1} 行缺少模型 ID")
            if not raw_in or not raw_out:
                raise ValueError(f"价格表 {model} 的输入/输出单价必须同时填写，或同时留空")
            try:
                input_price = float(raw_in)
                output_price = float(raw_out)
            except ValueError as exc:
                raise ValueError(f"价格表 {model} 的单价必须是数字") from exc
            prices[model] = {"input_per_million": input_price, "output_per_million": output_price}
        return prices

    # --------------------------------------------------------------- actions
    def _clear_secret(self, key: str, status_label: QLabel) -> None:
        answer = QMessageBox.question(self, "清除密钥", "确认清除本机保存的该密钥？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.context.secrets.delete(key)
        status_label.setText("尚未保存")
        QMessageBox.information(self, "已清除", "本机加密存储中的该密钥已删除。")

    def _choose_font(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择中文字体", "C:/Windows/Fonts", "字体 (*.ttf *.ttc *.otf)")
        if path:
            self.water_font_edit.setText(path)

    def _detect_latex(self) -> None:
        try:
            path = find_xelatex(self.context.settings)
        except FileNotFoundError as exc:
            self.latex_status.setText(str(exc))
            return
        if path:
            self.xelatex_edit.setText(str(path))
            version = xelatex_version(path)
            self.latex_status.setText(f"已找到：{path}\n{version}")
        else:
            self.latex_status.setText("未找到 xelatex；请安装 TeX Live 或填写完整路径。")

    def _update_latex_status(self) -> None:
        configured = self.context.settings.get("latex.xelatex_path", "")
        try:
            path = Path(configured) if configured else find_xelatex(self.context.settings)
        except FileNotFoundError as exc:
            self.latex_status.setText(str(exc))
            return
        if path and Path(path).exists():
            self.latex_status.setText(f"当前：{path}")
        else:
            self.latex_status.setText("尚未探测到 xelatex。")

    def _update_netdisk_status(self) -> None:
        ok, reason = self.context.netdisk_configured()
        secret = "已保存" if self.context.secrets.has("baidu_app_secret") else "未保存"
        self.baidu_status.setText(f"应用配置：{'完整' if ok else '不完整'}（SecretKey {secret}）\n{reason}")

    def _make_auth_url(self) -> None:
        if not self._save(notify=False):
            return
        try:
            url = self.context.netdisk().authorize_url()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "无法生成授权链接", f"{type(exc).__name__}: {exc}")
            return
        self.oauth_url_edit.setText(url)
        QDesktopServices.openUrl(QUrl(url))

    def _open_auth_url(self) -> None:
        url = self.oauth_url_edit.text().strip()
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _exchange_code(self) -> None:
        code, ok = QInputDialog.getText(self, "粘贴授权码", "百度授权页显示的 code（十分钟内有效）：")
        if not ok or not code.strip():
            return
        self._oauth_worker = OAuthWorker(self.context, code.strip())
        self._oauth_worker.succeeded.connect(lambda message: QMessageBox.information(self, "授权成功", message))
        self._oauth_worker.failed.connect(lambda message: QMessageBox.warning(self, "授权失败", message))
        self._oauth_worker.finished.connect(self._update_netdisk_status)
        self._oauth_worker.start()
        self.baidu_status.setText("正在交换授权码…")

    def _check_netdisk(self) -> None:
        self._check_worker = ConnectionWorker(self.context, mode="netdisk")
        self._check_worker.succeeded.connect(lambda message: QMessageBox.information(self, "网盘状态", message))
        self._check_worker.failed.connect(lambda message: QMessageBox.warning(self, "网盘检查失败", message))
        self._check_worker.start()
        self.baidu_status.setText("正在检查授权…")
