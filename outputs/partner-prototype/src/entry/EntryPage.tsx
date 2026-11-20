import type { ReactNode } from 'react';
import { product } from '../product';

export function EntryPage({ children }: { children: ReactNode }) {
  return <div className="entry-page">
    <section className="entry-intro" aria-labelledby="entry-title">
      <div className="entry-intro-top">
        <span className="entry-kicker">СИНОНИКС · ПОМОЩНИК ИНЖЕНЕРА</span>
        <span className="entry-product-mark" aria-hidden="true">↗</span>
      </div>
      <h1 id="entry-title">Опишите сети.<br />Соберите решение.</h1>
      <p className="entry-lead">Превратите описание инфраструктуры в проект информационного обмена — с уточнениями, правилами и схемой.</p>
      <a className="entry-jump" href="#login-title">Перейти ко входу <span aria-hidden="true">↓</span></a>
      <ol className="entry-process" aria-label="Как устроена работа">
        <li><span>01</span><strong>Контекст</strong><p>Сети, системы и задачи обмена</p></li>
        <li><span>02</span><strong>Уточнения</strong><p>Вопросы о недостающих условиях</p></li>
        <li><span>03</span><strong>Результат</strong><p>Объяснение, правила и draw.io</p></li>
      </ol>
      <div className="entry-start">
        <span className="entry-start-label">С ЧЕГО НАЧАТЬ</span>
        <p>Расскажите, какие сети участвуют, что нужно передавать и в какую сторону.</p>
      </div>
      <p className="entry-review-note">{product.disclaimer}.</p>
    </section>
    <div className="entry-access">{children}
      <p className="entry-access-note">Документы и проекты доступны в соответствии с правами вашей учётной записи.</p>
    </div>
  </div>;
}
