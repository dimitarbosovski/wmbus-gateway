CFLAGS = -std=c99 -Wall -Wextra -Wpedantic -Werror -O1 -g -fsanitize=address,undefined
SRC    = test_host.c ../firmware/hci.c ../firmware/imst_wmbus.c ../firmware/oms.c

all: run

vectors.h: gen_vectors.py
	python3 -I gen_vectors.py > vectors.h

test_host: vectors.h $(SRC)
	$(CC) $(CFLAGS) -o $@ $(SRC) -lmbedcrypto

SIM    = test_collector.c ../firmware/collector.c ../firmware/keystore.c ../firmware/hci.c \
         ../firmware/imst_wmbus.c ../firmware/oms.c

test_collector: vectors.h $(SIM)
	$(CC) $(CFLAGS) -o $@ $(SIM) -lmbedcrypto

run: test_host test_collector
	./test_host
	./test_collector

clean:
	rm -f test_host test_collector vectors.h
