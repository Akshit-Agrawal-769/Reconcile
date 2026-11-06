import { sentence } from "../format";

/** A server message, shown as written (first letter capitalised). */
export function ErrorBanner({ message }: { message: string | null | undefined }) {
  if (!message) return null;
  return <p className="error" role="alert">{sentence(message)}</p>;
}
