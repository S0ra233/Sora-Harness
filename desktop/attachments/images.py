"""复用已声明的 Qt 图像解码器；不加载界面，也不执行网络请求。"""

MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 24_000_000
MAX_EDGE = 1280
MAX_REQUEST_IMAGES = 8
MAX_REQUEST_BYTES = 20 * 1024 * 1024


def prepare_image(source: bytes) -> tuple[bytes, int, int]:
    """按真实内容解码、校正方向并缩放；动图明确只取第一帧。"""
    # Qt 仅在接收新图片时加载；纯文本 CLI 不要求初始化 GUI。
    from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
    from PySide6.QtGui import QImageReader

    if not source or len(source) > MAX_SOURCE_BYTES:
        raise ValueError("图片不能为空，原文件不得超过 20 MiB。")
    buffer = QBuffer()
    buffer.setData(QByteArray(source))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    try:
        reader = QImageReader(buffer)
        reader.setDecideFormatFromContent(True)
        reader.setAutoTransform(True)
        if bytes(reader.format()).lower() not in {b"png", b"jpeg", b"jpg", b"gif", b"webp"}:
            raise ValueError("只接收实际为 PNG、JPEG、GIF 或 WebP 的图片；后缀不作为判断依据。")
        size = reader.size()
        if size.width() <= 0 or size.height() <= 0 or size.width() * size.height() > MAX_PIXELS:
            raise ValueError("图片尺寸无效或超过 2400 万像素。")
        image = reader.read()
        if image.isNull():
            raise ValueError("图片解码失败，文件可能损坏或当前 Qt 缺少对应格式支持。")
    finally:
        buffer.close()
    if max(image.width(), image.height()) > MAX_EDGE:
        image = image.scaled(MAX_EDGE, MAX_EDGE, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    output = QBuffer()
    output.open(QIODevice.OpenModeFlag.WriteOnly)
    try:
        if not image.save(output, "PNG"):
            raise ValueError("无法生成模型输入图片。")
        prepared = bytes(output.data())
    finally:
        output.close()
    if len(prepared) > MAX_IMAGE_BYTES:
        raise ValueError("处理后的图片超过 8 MiB，请选择更小的图片。")
    return prepared, image.width(), image.height()
