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

// 版本由构建时注入：go build -ldflags "-X main.version=1.3.7"
// 源码里留 "dev"，避免忘了注入时误报一个看起来正常的版本。
var version = "dev"

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
	var stats, selftest, showVersion bool
	flag.BoolVar(&stats, "stats", false, "只统计")
	flag.BoolVar(&selftest, "selftest", false, "自测")
	flag.BoolVar(&showVersion, "version", false, "打印版本号后退出")
	flag.Parse()

	// 🔴 兜底：ldflags 写错（变量名不对、shell 把 ${VERSION} 展开成空串）
	//    时 version 会是 ""，`--version` 就只打印一个 "gt7-decrypt"，
	//    等于"声称有版本但看不出是哪版"。宁可显示 dev，也不要空白。
	if version == "" {
		version = "dev"
	}

	if showVersion {
		fmt.Printf("gt7-decrypt %s\n", version)
		return
	}
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
		// 🔴 os.Exit 不会跑 defer，bufio 里没刷出去的内容会**整段丢失**。
		//    这里的输出正是调用方用来定位问题的最后几条 FAIL 行，
		//    丢了之后只看得到"空输出 + 退出码 1"，无从下手。
		out.Flush()
		os.Exit(1)
	}
}

func runSelfTest() {
	// 🔴 这里曾经是个**自证循环**：
	//    旧代码用自己的 IV 加密一段明文，再交给 decrypt() 解回来，
	//    然后断言"解出来的 == 我放进去的"。而 decrypt() 的 IV 是从密文的
	//    0x40 读的 —— 加密时又把同一个值写在明文的 0x40 处，
	//    于是无论 IV 推导方向写反、还是密钥流偏移算错，它都能"通过"。
	//    实测：把 decrypt() 里的 iv1/iv2 对调，旧 selftest 依然打印 OK。
	//
	// 现在改成**外部生成的 golden vector**：
	//    由 tools/verify_salsa20.py 产出（Python 侧 Salsa20 + 正确的
	//    "nonce 取自密文" 构造），Go 这边只做"喂密文、比明文"。
	//    向量是死的常量，Go 无法自己捏一个能过的输入。
	//
	// 重新生成：
	//    cd tools && python -c "from verify_salsa20 import build_plain; \
	//      from probe_fields import encrypt_packet_pair; \
	//      c,p=encrypt_packet_pair(build_plain()); print(c[:80].hex()); print(p[:64].hex())"
	//    然后把下面两行常量替换掉（并同步 tools/verify_salsa20.py 的 seed）。
	//
	// 🔴 密文为什么是 **80** 字节而不是 64：
	//    这个协议把 IV 藏在密文的 0x40（=64）处，decrypt() 第一件事就是读它。
	//    向量只截前 64 字节 → 0x40 正好落在向量之外，补齐时被填成 0 →
	//    推出 iv1=0、密钥流全错、magic 校验失败。
	//    症状极具误导性：Python 侧拿**完整包**验证是好的，Go 却"解不开"，
	//    看起来像 Go 的 IV 推导写错了。实际只是向量没盖住 IV。
	//    80 同时覆盖 0x40..0x43 的 IV 与前 64 字节明文，够用且不至于贴一大坨。
	const (
		goldenCipherHex = "d732167a7a33c22cf0269d95b587111168ea08e3622871f2c6f8afd811462eb0" +
			"a2d2e1368744b15da2460d91e6cbc1b299008a8ba9ecab6e4d98716b1c9f746c" +
			"785634127a286a2f952857170cf4208b"
		goldenPlainHex = "3053374724d23172ef38e37b080e925770007ce01b72c0f98019cc8fe1558a50" +
			"5855492ea5e96d92b926d76001bc12af741642455b36cdd5e34fc6f8641f1eeb"
	)

	cipher, err := hex.DecodeString(goldenCipherHex)
	if err != nil {
		fmt.Println("SELFTEST FAILED: 内置向量解码失败:", err)
		os.Exit(1)
	}
	want, err := hex.DecodeString(goldenPlainHex)
	if err != nil {
		fmt.Println("SELFTEST FAILED: 期望明文解码失败:", err)
		os.Exit(1)
	}

	// 补齐到 296 字节：decrypt 要求 len >= 0x44+8，且真实包就是 296。
	// 补零不影响前 64 字节的比对（密钥流是按字节位置生成的）。
	//
	// 🔴 但前提是**向量本身已经盖住了 IV**（0x40..0x43）。补齐不会"补出"
	//    IV：补的是 0，于是 iv1=0，解出来必然是垃圾。这条断言就是防这个 ——
	//    否则下一个人改短向量时，只会得到一句"magic 校验没过"，查不到这里。
	if len(cipher) < 0x44 {
		fmt.Printf("SELFTEST FAILED: golden 向量只有 %d 字节，没盖到 IV(0x40..0x43)。\n"+
			"  请用 tools/verify_salsa20.py 重新生成 80 字节密文（见上方注释）。\n", len(cipher))
		os.Exit(1)
	}
	if len(cipher) < 296 {
		padded := make([]byte, 296)
		copy(padded, cipher)
		cipher = padded
	}

	got := decrypt(cipher)
	if got == nil {
		fmt.Println("SELFTEST FAILED: 解不开 golden vector（magic 校验没过 → IV 推导或密钥流错）")
		os.Exit(1)
	}
	if binary.LittleEndian.Uint32(got[:4]) != magicWanted {
		fmt.Println("SELFTEST FAILED: magic 不对")
		os.Exit(1)
	}
	for i := 0; i < len(want) && i < len(got); i++ {
		if got[i] != want[i] {
			fmt.Printf("SELFTEST FAILED: 明文第 %d 字节不符 got=%02x want=%02x\n",
				i, got[i], want[i])
			os.Exit(1)
		}
	}
	fmt.Println("SELFTEST OK")
}
