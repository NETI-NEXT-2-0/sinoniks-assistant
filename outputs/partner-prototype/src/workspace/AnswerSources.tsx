import { readWebSource } from '../api/web-source';
import { Icon } from '../ui';

export interface AnswerCitation { evidence_id: string; quote: string; title?: string; location?: string; web_source?: unknown }

export function AnswerSources({ citations }: { citations: AnswerCitation[] }) {
  if (!citations.length) return null;
  return <div className="message-sources" aria-label="Источники ответа">
    {citations.map((citation, index) => {
      const source = readWebSource(citation.web_source);
      return <details className="answer-source" key={`${citation.evidence_id}-${index}`}>
        <summary className="source-chip">
          <Icon name="doc" />{citation.title ?? `Источник ${index + 1}`}
        </summary>
        <blockquote>{citation.quote}</blockquote>
        {(citation.location || source) && <div className="answer-source-origin">
          {citation.location && <span>{citation.location}</span>}
          {source && <>
            <a href={source.url} target="_blank" rel="noopener noreferrer">Открыть страницу</a>
            <span>Снимок от <time dateTime={source.captured_on}>{source.captured_on.split('-').reverse().join('.')}</time></span>
          </>}
        </div>}
      </details>;
    })}
  </div>;
}
