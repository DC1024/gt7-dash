// GT7 遥测解密器 —— 独立可执行文件
//
// 单独做成二进制而非塞进 Python，原因：
// Salsa20 手写实现极易出错（我试过两次都算不出正确密钥流），
// 而 golang.org/x/crypto/salsa20 是经过验证的标准实现。
//
// 用法：
//   gt7-decrypt              从 stdin 读原始包，写明文到 stdout（hex 每行）
//   gt7-decrypt -stats       只输出统计信息
//   gt7-decrypt -selftest    自测
package main

import (
	"bufio"
	"encoding/binary"
	"encoding/hex"
	"flag"
	"fmt"
	"os"

	"golang.org/x/crypto/salsa20"
)

var key = [32]byte([]byte("Simulator Interface Packet GT7 ver 0.0"))

const (
	magicWanted = 0x47375330 // "GT7\0"
	ivMask     = 0xDEADBEAF
)

// decrypt 解一个包。返回明文，失败返回 nil。
func decrypt(dat []byte) []byte {
	if len(dat) < 0x44+8 {
		return nil
	}
	oiv := dat[0x40:0x44]
	iv1 := binary.LittleEndian.Uint32(oiv)
	iv2 := iv1 ^ ivMask
	iv := make([]byte, 8)
	binary.LittleEndian.PutUint32(iv, iv2)
	binary.LittleEndian.PutUint32(iv[4:], iv1)

	d := make([]byte, len(dat))
	salsa20.XORKeyStream(d, dat, iv, &key)

	if binary.LittleEndian.Uint32(d[:4]) != magicWanted {
		return nil
	}
	return d
}

func main() {
	var stats, selftest bool
	flag.BoolVar(&stats, "stats", false, "只统计")
	flag.BoolVar(&selftest, "selftest", false, "自测")
	flag.Parse()

	if selftest {
		runSelfTest()
		return
	}

	in := bufio.NewScanner(os.Stdin)
	in.Buffer(make([]byte, 1024*1024), 1024*1024)
	out := bufio.NewWriter(os.Stdout)
	defer out.Flush()

	ok, bad := 0, 0
	for in.Scan() {
		line := in.Text()
		if len(line) < 16 {
			continue
		}
		raw, err := hex.DecodeString(line)
		if err != nil {
			bad++
			continue
		}
		p := decrypt(raw)
		if p == nil {
			bad++
			if !stats {
				fmt.Fprintln(out, "FAIL")
			}
			continue
		}
		ok++
		if !stats {
			fmt.Fprintln(out, hex.EncodeToString(p))
		}
	}
	if stats {
		fmt.Fprintf(os.Stderr, "decrypt: ok=%d bad=%d\n", ok, bad)
	}
	if ok == 0 {
		os.Exit(1)
	}
}

func runSelfTest() {
	// 构造一个已知明文，加密再解密，验证往返
	plain := make([]byte, 296)
	binary.LittleEndian.PutUint32(plain[0:], magicWanted)
	plain[4] = 1
	plain[5] = 0
	binary.LittleEndian.PutUint16(plain[6:], 0x0F)
	binary.LittleEndian.PutUint32(plain[8:], 12345)
	iv1 := uint32(0xDEADBEEF)
	binary.LittleEndian.PutUint32(plain[0x40:], iv1^ivMask)

	iv := make([]byte, 8)
	binary.LittleEndian.PutUint32(iv, iv1^ivMask^ivMask) // = iv1^mask
	binary.LittleEndian.PutUint32(iv[4:], iv1)
	enc := make([]byte, len(plain))
	salsa20.XORKeyStream(enc, plain, iv, &key)

	got := decrypt(enc)
	if got == nil {
		fmt.Println("SELFTEST FAILED: 无法解回自己加密的包")
		os.Exit(1)
	}
	if binary.LittleEndian.Uint32(got[:4]) != magicWanted {
		fmt.Println("SELFTEST FAILED: magic 不对")
		os.Exit(1)
	}
	if binary.LittleEndian.Uint32(got[8:]) != 12345 {
		fmt.Println("SELFTEST FAILED: seq 不对")
		os.Exit(1)
	}
	fmt.Println("SELFTEST OK")
}
