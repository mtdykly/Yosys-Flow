module demo(input a, b, c, output y);
wire t;
assign t = a & b;
assign y = t ^ c;
endmodule
