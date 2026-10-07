import { useCallback, useEffect, useRef, useState } from "react";

export interface AsyncState<T> {
  data: T | undefined;
  error: Error | undefined;
  loading: boolean;
  reload: () => void;
}

/** Runs `fn` whenever `deps` change; ignores results of stale calls. */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]): AsyncState<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<Error>();
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const call = useRef(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    const id = ++call.current;
    setLoading(true);
    setError(undefined);
    fn().then(
      (d) => {
        if (id === call.current) {
          setData(d);
          setLoading(false);
        }
      },
      (e: unknown) => {
        if (id === call.current) {
          setError(e instanceof Error ? e : new Error(String(e)));
          setLoading(false);
        }
      },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  return { data, error, loading, reload };
}
