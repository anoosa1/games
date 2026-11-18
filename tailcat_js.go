// Bind Tailcat byte streams to the browser. Build with GOOS=js GOARCH=wasm.
package main

import (
	"context"
	"errors"
	"io"
	"net"
	"sync"
	"syscall/js"
	"time"

	"github.com/tailscale/tailcat"
	"tailscale.com/types/logger"
)

const gamePort = 5656

func promise(work func() (any, error)) js.Value {
	run := js.FuncOf(func(_ js.Value, args []js.Value) any {
		resolve, reject := args[0], args[1]
		go func() {
			value, err := work()
			if err != nil {
				reject.Invoke(js.Global().Get("Error").New(err.Error()))
				return
			}
			resolve.Invoke(value)
		}()
		return nil
	})
	result := js.Global().Get("Promise").New(run)
	run.Release()
	return result
}

func stream(conn net.Conn, cleanup func()) js.Value {
	var closeOnce sync.Once
	var fn []js.Func
	read := js.FuncOf(func(_ js.Value, _ []js.Value) any {
		return promise(func() (any, error) {
			var buf [4096]byte
			n, err := conn.Read(buf[:])
			if n > 0 {
				out := js.Global().Get("Uint8Array").New(n)
				js.CopyBytesToJS(out, buf[:n])
				return out, nil
			}
			if errors.Is(err, io.EOF) {
				return js.Null(), nil
			}
			return js.Null(), err
		})
	})
	write := js.FuncOf(func(_ js.Value, args []js.Value) any {
		data := make([]byte, args[0].Length())
		js.CopyBytesToGo(data, args[0])
		return promise(func() (any, error) {
			conn.SetWriteDeadline(time.Now().Add(12 * time.Second))
			_, err := conn.Write(data)
			return js.Undefined(), err
		})
	})
	closeStream := js.FuncOf(func(_ js.Value, _ []js.Value) any {
		closeOnce.Do(func() {
			conn.Close()
			cleanup()
			for _, f := range fn {
				f.Release()
			}
		})
		return nil
	})
	fn = []js.Func{read, write, closeStream}
	return js.ValueOf(map[string]any{"read": read, "write": write, "close": closeStream})
}

func listen(_ js.Value, args []js.Value) any {
	onConnection := args[0]
	return promise(func() (any, error) {
		srv := &tailcat.Server{Logf: logger.Discard}
		srv.OnTCP = func(port uint16) func(net.Conn) {
			if port != gamePort {
				return nil
			}
			return func(conn net.Conn) {
				onConnection.Invoke(stream(conn, func() {}))
			}
		}
		if err := srv.Start(); err != nil {
			srv.Close()
			return nil, err
		}
		var closeServer js.Func
		closeServer = js.FuncOf(func(_ js.Value, _ []js.Value) any {
			srv.Close()
			closeServer.Release()
			return nil
		})
		return map[string]any{"address": string(srv.TailcatAddr()), "close": closeServer}, nil
	})
}

func dial(_ js.Value, args []js.Value) any {
	address := args[0].String()
	return promise(func() (any, error) {
		client := &tailcat.Client{Server: tailcat.Addr(address), Logf: logger.Discard}
		ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
		defer cancel()
		for {
			attempt, stop := context.WithTimeout(ctx, 4*time.Second)
			_, err := client.Ping(attempt)
			stop()
			if err == nil {
				break
			}
			if !errors.Is(err, context.DeadlineExceeded) || ctx.Err() != nil {
				client.Close()
				return nil, err
			}
		}
		conn, err := client.DialTCPPort(ctx, gamePort)
		if err != nil {
			client.Close()
			return nil, err
		}
		return stream(conn, func() { client.Close() }), nil
	})
}

func main() {
	js.Global().Set("tabletopListen", js.FuncOf(listen))
	js.Global().Set("tabletopDial", js.FuncOf(dial))
	js.Global().Call("tabletopReady")
	select {}
}
