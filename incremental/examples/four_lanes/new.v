module mix(input [7:0] a, b, c, output [7:0] y);
  wire [7:0] t;
  assign t = a & b;
  assign y = t ^ c;
endmodule

module mix_edited(input [7:0] a, b, c, output [7:0] y);
  wire [7:0] t;
  assign t = a | b;
  assign y = t ^ c;
endmodule

module lane(input [7:0] data, key, mask, salt, output [7:0] result, side);
  wire [7:0] pre;
  assign pre = data ^ key;
  mix core(pre, mask, salt, result);
  assign side = pre & salt;
endmodule

module lane_edited(input [7:0] data, key, mask, salt, output [7:0] result, side);
  wire [7:0] pre;
  assign pre = data ^ key;
  mix_edited core(pre, mask, salt, result);
  assign side = pre & salt;
endmodule

module chip(input [31:0] data, key, mask, salt,
            output [31:0] result, side, output [7:0] summary);
  lane u0(data[7:0],   key[7:0],   mask[7:0],   salt[7:0],   result[7:0],   side[7:0]);
  lane_edited u1(data[15:8], key[15:8], mask[15:8], salt[15:8], result[15:8], side[15:8]);
  lane u2(data[23:16], key[23:16], mask[23:16], salt[23:16], result[23:16], side[23:16]);
  lane u3(data[31:24], key[31:24], mask[31:24], salt[31:24], result[31:24], side[31:24]);
  assign summary = result[7:0] ^ result[15:8] ^ result[23:16] ^ result[31:24];
endmodule
