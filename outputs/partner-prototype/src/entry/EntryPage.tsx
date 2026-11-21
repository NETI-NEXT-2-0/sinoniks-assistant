import type { ReactNode } from 'react';
import { product } from '../product';

interface Props {
  children: ReactNode;
  onLogin?: () => void;
  onRegister?: () => void;
  busy?: boolean;
}

export function EntryPage({ children, onLogin, onRegister, busy }: Props) {
  return <div className="entry-page">
    <section className="entry-intro" aria-labelledby="entry-title">
      <h1 id="entry-title">Опишите сети.<br />Соберите решение.</h1>
      <p className="entry-lead">Опишите обмен между двумя контурами. Синоникс уточнит недостающие параметры и подготовит правила и схему для проверки инженером.</p>
      {onLogin && <nav className="entry-actions" aria-label="Доступ к рабочему пространству">
        {onRegister && <a className="entry-jump" href="#login-title" aria-disabled={busy} onClick={(event) => {
          event.preventDefault();
          onRegister();
        }}>Создать аккаунт</a>}
        <a className={onRegister ? 'entry-jump entry-jump-secondary' : 'entry-jump'} href="#login-title" aria-disabled={busy} onClick={(event) => {
          event.preventDefault();
          onLogin();
        }}>Перейти ко входу</a>
      </nav>}
      <ol className="entry-process" aria-label="Как устроена работа">
        <li><strong>Опишите задачу</strong><p>Укажите системы, протоколы и направление передачи.</p></li>
        <li><strong>Уточните условия</strong><p>Ответьте на вопросы по недостающим параметрам.</p></li>
        <li><strong>Проверьте проект</strong><p>Сверьте правила и выгрузите JSON или схему draw.io.</p></li>
      </ol>
      <p className="entry-review-note">{product.disclaimer}.</p>
    </section>
    <div className="entry-access">{children}
      <p className="entry-access-note">Документы и проекты доступны в соответствии с правами вашей учётной записи.</p>
    </div>
    <section className="entry-example" aria-labelledby="entry-example-title">
      <div>
        <h2 id="entry-example-title">Пример задачи</h2>
        <p>«Сервер в контуре А передаёт журналы на коллектор в контуре Б по UDP 514».</p>
      </div>
      <div className="entry-flow" role="img" aria-label="Поток UDP 514 от сервера контура А к коллектору контура Б">
        <div><span>Контур А</span><strong>Сервер</strong></div>
        <span className="entry-flow-link">UDP 514</span>
        <div><span>Контур Б</span><strong>Коллектор</strong></div>
      </div>
    </section>
  </div>;
}
