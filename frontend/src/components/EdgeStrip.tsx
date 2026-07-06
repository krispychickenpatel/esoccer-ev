// Signature element: model probability vs book implied probability as paired
// bars — the "edge" is the visible gap between green and grey.
export default function EdgeStrip({ model, book }: { model: number; book: number }) {
  const w = (p: number) => `${Math.min(100, p * 100)}%`
  return (
    <div className="edge-strip" title={`model ${(model * 100).toFixed(1)}% vs book ${(book * 100).toFixed(1)}%`}>
      <div className="edge-row">
        <span className="lbl">model</span>
        <div className="bar model" style={{ width: w(model) }} />
        <span>{(model * 100).toFixed(0)}%</span>
      </div>
      <div className="edge-row">
        <span className="lbl">book</span>
        <div className="bar book" style={{ width: w(book) }} />
        <span>{(book * 100).toFixed(0)}%</span>
      </div>
    </div>
  )
}
