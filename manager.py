import os
from sqlalchemy import create_engine, Column, Integer, String, MetaData, Table, Boolean
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError, OperationalError

# --- НАСТРОЙКИ (ЗАПОЛНИТЕ САМИ) ---
# Вставьте сюда "External Connection String" из настроек базы данных на Render
DATABASE_URL = "Ваша External Connection String ссылка из Render"
# --- КОНЕЦ НАСТРОЕК ---

try:
    connect_args = {}
    if DATABASE_URL.startswith('postgresql://'):
        connect_args['sslmode'] = 'require' 
        
    engine = create_engine(DATABASE_URL, connect_args=connect_args)
    
    metadata = MetaData()
    # _ИЗМЕНЕНО_: СТРУКТУРА ТАБЛИЦЫ ПОЛНОСТЬЮ СИНХРОНИЗИРОВАНА С duty_bot.py
    students = Table('students', metadata,
        Column('id', Integer, primary_key=True),
        Column('user_id', String(100), unique=True, nullable=True), # _ДОБАВЛЕНО_
        Column('name', String(100), nullable=False),
        Column('username', String(100), unique=True, nullable=False),
        Column('duty_count', Integer, default=0),
        Column('duty_debt', Integer, default=0),
        Column('chat_id', String(100), nullable=True),
        Column('is_active', Boolean, default=True),
        Column('last_duty_turn', Integer, default=0),
        Column('streak', Integer, default=0),
        Column('skip_next_turn', Boolean, default=False),
        Column('has_immunity', Integer, default=0)
    )
    
    metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db_session = Session()
    print("✅ Успешное подключение к базе данных.")
    print("   (Если колонка 'user_id' отсутствовала, она была добавлена автоматически)")
except Exception as e:
    print(f"❌ Ошибка подключения к базе данных: {e}")
    exit()

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def display_students(all_students):
    print("\nТекущий список:")
    if not all_students:
        print("  (пусто)")
    else:
        for i, user in enumerate(all_students, 1):
            status = " (Неактивен)" if not user.is_active else ""
            immunity = f" (Отдых: {user.has_immunity} 🛡️)" if user.has_immunity > 0 else ""
            cooldown = f" (Ход: {user.last_duty_turn})"
            # _ИЗМЕНЕНО_: Отображение user_id
            user_id_info = f" (ID: {user.user_id})" if user.user_id else " (ID: не привязан)"
            
            print(f"  {i}. {user.name} ({user.username}){user_id_info} - Дежурств: {user.duty_count}, Долг: {user.duty_debt}{status}{immunity}{cooldown}")

def handle_single_add():
    name = input("Введите Имя Фамилию: ").strip()
    username = input("Введите username (начиная с @): ").strip()
    if name and username.startswith('@'):
        try:
            db_session.execute(students.insert().values(name=name, username=username, has_immunity=0))
            db_session.commit()
            print(f"\n✅ Пользователь {name} добавлен.")
        except IntegrityError:
            db_session.rollback()
            print(f"\n⚠️ Ошибка: Пользователь с username {username} уже существует.")
    else:
        print("\n❌ Неверный формат.")
    input("\nНажмите Enter для продолжения...")

def handle_bulk_add():
    print("\nВведите список пользователей (Имя Фамилия @username), каждый с новой строки.")
    print("Для завершения введите пустую строку и нажмите Enter.")
    lines = []
    while True:
        line = input("> ").strip()
        if not line: break
        lines.append(line)
    if not lines:
        print("\nОтменено.")
        input("\nНажмите Enter для продолжения...")
        return
    added_count = 0
    skipped_count = 0
    for line in lines:
        try:
            parts = line.split('@')
            if len(parts) != 2 or not parts[0].strip() or not parts[1].strip(): 
                skipped_count += 1
                continue
            name = parts[0].strip()
            username = "@" + parts[1].strip()
            if db_session.query(students).filter(students.c.username == username).first():
                skipped_count += 1
                continue
            db_session.execute(students.insert().values(name=name, username=username, duty_count=0, duty_debt=0, is_active=True, has_immunity=0))
            added_count += 1
        except Exception:
            db_session.rollback()
            skipped_count += 1
    try:
        db_session.commit()
        print(f"\n✅ Массовое добавление завершено!")
        print(f"Добавлено новых пользователей: {added_count}")
        print(f"Пропущено: {skipped_count}")
    except Exception as e:
        db_session.rollback()
        print(f"\n❌ Критическая ошибка при сохранении: {e}")
    input("\nНажмите Enter для продолжения...")

def main():
    while True:
        clear_screen()
        print("--- Менеджер списка студентов (База данных) ---")
        
        try:
            all_students = db_session.query(students).order_by(students.c.name).all()
        except OperationalError as e:
            print("\n!!! КРИТИЧЕСКАЯ ОШИБКА: Структура БД не соответствует коду.")
            print(f"Ошибка: {e}")
            print("Возможно, вам нужно вручную удалить старые таблицы в вашей БД и перезапустить этот скрипт.")
            input("\nНажмите Enter для выхода...")
            return

        display_students(all_students)
        
        print("\nВыберите действие:")
        print("  1. ➕ Добавить пользователя (одного)")
        print("  2. ➕ Массовое добавление")
        print("  3. ➖ Удалить пользователя")
        print("  4. ✏️ Изменить счетчик дежурств")
        print("  5. 💸 Изменить счетчик долгов")
        print("  6. 🔄 Изменить статус активности")
        print("  7. 🛡️ Изменить счетчик иммунитета (Отдых)")
        print("  8. ⏳ Изменить последний ход дежурства (Кулдаун)")
        # _НОВОЕ_: Новый пункт меню
        print("  9. 🆔 Изменить User ID")
        print(" 10. 🚪 Выйти")
        
        choice = input("\nВаш выбор: ")

        if choice in ['1', '2', '3', '4', '5', '6', '7', '8', '9']:
            if choice != '1' and choice != '2' and not all_students:
                print("\nСписок пуст, нечего редактировать.")
                input("\nНажмите Enter для продолжения...")
                continue
            try:
                if choice == '1':
                    handle_single_add()
                elif choice == '2':
                    handle_bulk_add()
                elif choice == '3':
                    num_to_remove = int(input("Введите номер пользователя для удаления: "))
                    if 1 <= num_to_remove <= len(all_students):
                        user_to_remove = all_students[num_to_remove - 1]
                        db_session.query(students).filter(students.c.id == user_to_remove.id).delete()
                        db_session.commit()
                        print(f"\n✅ Пользователь {user_to_remove.name} удален.")
                    else: print("\n❌ Неверный номер.")
                elif choice == '4':
                    num_to_edit = int(input("Введите номер пользователя для редактирования дежурств: "))
                    if 1 <= num_to_edit <= len(all_students):
                        user_to_edit = all_students[num_to_edit - 1]
                        new_count = int(input(f"Введите новое количество дежурств для {user_to_edit.name} (текущее: {user_to_edit.duty_count}): "))
                        db_session.execute(students.update().where(students.c.id == user_to_edit.id).values(duty_count=new_count))
                        db_session.commit()
                        print(f"\n✅ Счетчик дежурств для {user_to_edit.name} обновлен на {new_count}.")
                    else: print("\n❌ Неверный номер.")
                elif choice == '5':
                    num_to_edit = int(input("Введите номер пользователя для редактирования долга: "))
                    if 1 <= num_to_edit <= len(all_students):
                        user_to_edit = all_students[num_to_edit - 1]
                        new_debt = int(input(f"Введите новое количество долгов для {user_to_edit.name} (текущее: {user_to_edit.duty_debt}): "))
                        db_session.execute(students.update().where(students.c.id == user_to_edit.id).values(duty_debt=new_debt))
                        db_session.commit()
                        print(f"\n✅ Счетчик долгов для {user_to_edit.name} обновлен на {new_debt}.")
                    else: print("\n❌ Неверный номер.")
                elif choice == '6':
                    num_to_edit = int(input("Введите номер пользователя для изменения статуса активности: "))
                    if 1 <= num_to_edit <= len(all_students):
                        user_to_edit = all_students[num_to_edit - 1]
                        new_status = not user_to_edit.is_active
                        db_session.execute(students.update().where(students.c.id == user_to_edit.id).values(is_active=new_status))
                        db_session.commit()
                        status_text = "Активен" if new_status else "Неактивен"
                        print(f"\n✅ Статус активности для {user_to_edit.name} обновлен на: {status_text}.")
                    else: print("\n❌ Неверный номер.")
                elif choice == '7':
                    num_to_edit = int(input("Введите номер пользователя для изменения счетчика иммунитета: "))
                    if 1 <= num_to_edit <= len(all_students):
                        user_to_edit = all_students[num_to_edit - 1]
                        new_immunity_turns = int(input(f"Введите новое количество ходов иммунитета для {user_to_edit.name} (текущее: {user_to_edit.has_immunity}): "))
                        db_session.execute(students.update().where(students.c.id == user_to_edit.id).values(has_immunity=new_immunity_turns))
                        db_session.commit()
                        print(f"\n✅ Счетчик иммунитета для {user_to_edit.name} обновлен на: {new_immunity_turns} ход(а).")
                    else: print("\n❌ Неверный номер.")
                elif choice == '8':
                    num_to_edit = int(input("Введите номер пользователя для изменения хода дежурства: "))
                    if 1 <= num_to_edit <= len(all_students):
                        user_to_edit = all_students[num_to_edit - 1]
                        new_turn = int(input(f"Введите новый ход дежурства (0 для сброса кулдауна) для {user_to_edit.name} (текущий: {user_to_edit.last_duty_turn}): "))
                        db_session.execute(students.update().where(students.c.id == user_to_edit.id).values(last_duty_turn=new_turn))
                        db_session.commit()
                        print(f"\n✅ Последний ход дежурства для {user_to_edit.name} обновлен на {new_turn}.")
                    else: print("\n❌ Неверный номер.")
                # _НОВОЕ_: Логика для изменения User ID
                elif choice == '9':
                    num_to_edit = int(input("Введите номер пользователя для изменения User ID: "))
                    if 1 <= num_to_edit <= len(all_students):
                        user_to_edit = all_students[num_to_edit - 1]
                        new_user_id = input(f"Введите новый User ID для {user_to_edit.name} (текущий: {user_to_edit.user_id or 'пусто'}): ").strip()
                        db_session.execute(students.update().where(students.c.id == user_to_edit.id).values(user_id=new_user_id))
                        db_session.commit()
                        print(f"\n✅ User ID для {user_to_edit.name} обновлен на {new_user_id}.")
                    else: print("\n❌ Неверный номер.")
            except ValueError:
                print("\n❌ Пожалуйста, введите число.")
            except IntegrityError:
                db_session.rollback()
                print(f"\n⚠️ Ошибка: Этот User ID или username уже занят другим пользователем.")
            input("\nНажмите Enter для продолжения...")
        
        elif choice == '10':
            print("\nДо свидания!")
            break
        else:
            print("\nНеверный выбор.")
            input("\nНажмите Enter для продолжения...")

if __name__ == "__main__":
    main()  