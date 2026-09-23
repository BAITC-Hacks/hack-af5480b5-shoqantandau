"""Роли и демо-пользователи — создаются автоматически командой migrate,
чтобы проверяющий мог войти сразу после установки по README."""
from django.contrib.auth.hashers import make_password
from django.db import migrations

PERMS = {
    "run_calculation": "Запускать расчёт заказов",
    "edit_order": "Править количество и утверждать заказ",
    "export_order": "Скачивать заказ для 1С",
    "manage_data": "Загружать выгрузки 1С",
}
ROLES = {
    "Администратор": ["run_calculation", "edit_order", "export_order", "manage_data"],
    "Менеджер закупа": ["run_calculation", "edit_order", "export_order"],
    "Наблюдатель": [],
}
USERS = [  # логин, пароль, роль, имя, администратор
    ("admin", "admin12345", "Администратор", "Администратор", True),
    ("manager", "manager12345", "Менеджер закупа", "Айгерим (закуп)", False),
    ("viewer", "viewer12345", "Наблюдатель", "Ерлан (руководитель)", False),
]


def forwards(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")
    User = apps.get_model("auth", "User")
    ct, _ = ContentType.objects.get_or_create(app_label="procurement", model="calculationrun")
    perms = {code: Permission.objects.get_or_create(codename=code, content_type=ct, defaults={"name": name})[0]
             for code, name in PERMS.items()}
    groups = {}
    for role, codes in ROLES.items():
        g, _ = Group.objects.get_or_create(name=role)
        g.permissions.set([perms[c] for c in codes])
        groups[role] = g
    for login, pwd, role, full, staff in USERS:
        if User.objects.filter(username=login).exists():
            continue
        # администратор — суперпользователь: управляет пользователями и ролями в разделе «Пользователи»
        u = User.objects.create(username=login, password=make_password(pwd), first_name=full,
                                is_staff=staff, is_superuser=staff)
        u.groups.add(groups[role])


def backwards(apps, schema_editor):
    apps.get_model("auth", "User").objects.filter(username__in=[u[0] for u in USERS]).delete()
    apps.get_model("auth", "Group").objects.filter(name__in=list(ROLES)).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("procurement", "0004_roles_audit"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]
    operations = [migrations.RunPython(forwards, backwards)]
