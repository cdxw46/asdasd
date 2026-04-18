package wavplay

import (
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"os"
	"time"

	"github.com/pion/rtp"

	"github.com/smurf/pbx/internal/audio"
)

// StreamWAVPCMU reads 8 kHz mono 16-bit LE PCM WAV and sends 20 ms PCMU RTP frames to remoteHost:remotePort from localBindIP:0.
func StreamWAVPCMU(path, localBindIP, remoteHost string, remotePort int, stop <-chan struct{}) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	dataOff, err := findWAVDataOffset(f)
	if err != nil {
		return err
	}
	if _, err := f.Seek(dataOff, 0); err != nil {
		return err
	}

	ip := net.ParseIP(localBindIP)
	if ip == nil {
		ip = net.IPv4zero
	}
	pc, err := net.ListenUDP("udp", &net.UDPAddr{IP: ip, Port: 0})
	if err != nil {
		return err
	}
	defer pc.Close()
	raddr, err := net.ResolveUDPAddr("udp", fmt.Sprintf("%s:%d", remoteHost, remotePort))
	if err != nil {
		return err
	}

	pcm := make([]byte, 320) // 160 samples * 2 bytes
	var seq uint16
	var ts uint32
	const ssrc = 0x534d5250
	ticker := time.NewTicker(20 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-stop:
			return nil
		case <-ticker.C:
			_, err := io.ReadFull(f, pcm)
			if err != nil {
				if err == io.EOF || err == io.ErrUnexpectedEOF {
					if _, err := f.Seek(dataOff, 0); err != nil {
						return nil
					}
					continue
				}
				return err
			}
			payload := make([]byte, 160)
			for i := 0; i < 160; i++ {
				s := int16(binary.LittleEndian.Uint16(pcm[i*2 : i*2+2]))
				payload[i] = audio.ULawEncode(s)
			}
			pkt := rtp.Packet{
				Header: rtp.Header{
					Version: 2, PayloadType: 0, SequenceNumber: seq,
					Timestamp: ts, SSRC: ssrc,
				},
				Payload: payload,
			}
			seq++
			ts += 160
			b, err := pkt.Marshal()
			if err != nil {
				continue
			}
			_, _ = pc.WriteToUDP(b, raddr)
		}
	}
}

func findWAVDataOffset(f *os.File) (int64, error) {
	if _, err := f.Seek(0, 0); err != nil {
		return 0, err
	}
	hdr := make([]byte, 12)
	if _, err := io.ReadFull(f, hdr); err != nil {
		return 0, err
	}
	if string(hdr[0:4]) != "RIFF" || string(hdr[8:12]) != "WAVE" {
		return 0, fmt.Errorf("not a RIFF WAVE file")
	}
	for {
		var chunkID [4]byte
		if _, err := io.ReadFull(f, chunkID[:]); err != nil {
			return 0, err
		}
		var sz uint32
		if err := binary.Read(f, binary.LittleEndian, &sz); err != nil {
			return 0, err
		}
		switch string(chunkID[:]) {
		case "fmt ":
			fmtb := make([]byte, sz)
			if _, err := io.ReadFull(f, fmtb); err != nil {
				return 0, err
			}
			if len(fmtb) < 16 {
				return 0, fmt.Errorf("short fmt")
			}
			if binary.LittleEndian.Uint16(fmtb[0:2]) != 1 {
				return 0, fmt.Errorf("only PCM supported")
			}
			if binary.LittleEndian.Uint16(fmtb[2:4]) != 1 {
				return 0, fmt.Errorf("only mono")
			}
			if binary.LittleEndian.Uint32(fmtb[4:8]) != 8000 {
				return 0, fmt.Errorf("only 8000 Hz")
			}
			if binary.LittleEndian.Uint16(fmtb[14:16]) != 16 {
				return 0, fmt.Errorf("only 16-bit")
			}
		case "data":
			pos, _ := f.Seek(0, io.SeekCurrent)
			return pos, nil
		default:
			if _, err := f.Seek(int64(sz), io.SeekCurrent); err != nil {
				return 0, err
			}
		}
	}
}
