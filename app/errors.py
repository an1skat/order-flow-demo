"""Типи помилок, від яких залежить, що робить конвеєр далі."""


class IntegrationError(Exception):
    """Базова помилка зовнішнього сервісу."""


class TransientError(IntegrationError):
    """Тимчасовий збій (таймаут, 5xx). Можна повторити."""


class PermanentError(IntegrationError):
    """Помилка в даних. Повтор не допоможе, потрібна людина."""


class ShiftClosedError(IntegrationError):
    """Каса Checkbox: зміна не відкрита. Відкриваємо і пробуємо ще раз."""
