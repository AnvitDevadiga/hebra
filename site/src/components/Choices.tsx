interface Props {
  legend: string;
  name: string;
  value: string;
  options: [string, string][];
  onChange: (value: string) => void;
}

/** A group of native radio buttons styled as segments (keyboard and screen-reader friendly). */
export function Choices({ legend, name, value, options, onChange }: Props) {
  return (
    <fieldset className="choices">
      <legend>{legend}</legend>
      <div className="segments">
        {options.map(([id, text]) => (
          <label key={id}>
            <input
              type="radio"
              name={name}
              value={id}
              checked={value === id}
              onChange={() => onChange(id)}
            />
            <span>{text}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}
